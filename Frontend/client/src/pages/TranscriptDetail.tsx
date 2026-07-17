import { useParams, Link, useLocation } from "wouter";
import { ArrowLeft, Share2, Download, Copy, Play, Search, Clock, Calendar, Pencil, Check, Loader2, Sparkles, FileText, Square } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from "@/components/ui/accordion";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useToast } from "@/hooks/use-toast";
import { useState, useEffect, useRef, useLayoutEffect, useCallback, useMemo } from "react";
import { apiUrl } from "@/lib/backend";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";
import ReactMarkdown from "react-markdown";
import { QueryErrorState } from "@/components/ui/query-error-state";
import { SummaryTableOfContents, TranscriptSummaryDocument } from "@/components/transcript-summary-document";
import { DesktopTitleBar } from "@/components/DesktopTitleBar";
import { useTranscriptAutoRefresh } from "@/hooks/use-transcript-auto-refresh";
import { extractFailureMessage, friendlyError, friendlyRequestMessage, responseErrorMessage } from "@/lib/request-errors";
import { prepareSummaryHtml } from "@/lib/summary-html";
import type { SettingsResponse, TranscriptDetailResponse, TranscriptHistoryItem } from "@/lib/api-types";
import { useI18n } from "@/i18n";

function normalizeSummaryMarkdown(text: string): string {
  return (text || "")
    .replace(/\r\n?/g, "\n")
    .replace(/\u00a0/g, " ")
    .replace(/\u200b/g, "")
    .replace(/^([ \t]*)[•●◦▪▫‣⁃]\s+/gm, "$1- ")
    .trim();
}

function localizedProcessingStep(
  value: string | null | undefined,
  fallback: string,
  t: ReturnType<typeof useI18n>["t"],
  formatNumber: ReturnType<typeof useI18n>["formatNumber"],
): string {
  const source = String(value || fallback).trim();
  const retryMatch = /^Retrying in ([\d.,]+)s \((\d+)\/(\d+)\)$/.exec(source);
  if (retryMatch) {
    const seconds = Number(retryMatch[1].replace(",", "."));
    return t("Retrying in {{seconds}}s ({{attempt}}/{{total}})", {
      seconds: Number.isFinite(seconds) ? formatNumber(seconds, { maximumFractionDigits: 2 }) : retryMatch[1],
      attempt: formatNumber(Number(retryMatch[2])),
      total: formatNumber(Number(retryMatch[3])),
    });
  }

  const downloadMatch = /^Downloading\.\.\.\s+([\d.,]+)%(.*)$/.exec(source);
  if (downloadMatch) {
    const percentage = Number(downloadMatch[1].replace(",", "."));
    const formattedPercentage = Number.isFinite(percentage)
      ? formatNumber(percentage / 100, { style: "percent", maximumFractionDigits: 1 })
      : `${downloadMatch[1]}%`;
    const technicalSuffix = downloadMatch[2].replace(" • ETA ", ` • ${t("ETA")} `);
    return `${t("Downloading… {{percent}}", { percent: formattedPercentage })}${technicalSuffix}`;
  }

  if (source.startsWith("Error: ")) {
    return `${t("Error")}: ${source.slice("Error: ".length)}`;
  }
  return t(source);
}

// Speaker colors for diarization - visually distinct palette
const SPEAKER_COLORS = [
  { bg: "bg-blue-100 dark:bg-blue-900/40", text: "text-blue-700 dark:text-blue-300", border: "border-blue-300 dark:border-blue-700" },
  { bg: "bg-emerald-100 dark:bg-emerald-900/40", text: "text-emerald-700 dark:text-emerald-300", border: "border-emerald-300 dark:border-emerald-700" },
  { bg: "bg-amber-100 dark:bg-amber-900/40", text: "text-amber-700 dark:text-amber-300", border: "border-amber-300 dark:border-amber-700" },
  { bg: "bg-purple-100 dark:bg-purple-900/40", text: "text-purple-700 dark:text-purple-300", border: "border-purple-300 dark:border-purple-700" },
  { bg: "bg-rose-100 dark:bg-rose-900/40", text: "text-rose-700 dark:text-rose-300", border: "border-rose-300 dark:border-rose-700" },
  { bg: "bg-cyan-100 dark:bg-cyan-900/40", text: "text-cyan-700 dark:text-cyan-300", border: "border-cyan-300 dark:border-cyan-700" },
];

// Component to render transcript with speaker diarization labels
function SpeakerFormattedText({ content }: { content: string }) {
  const { formatNumber, t } = useI18n();
  const hasSpeakerLabels = useMemo(
    () => /\[Speaker (\d+)\]:/.test(content),
    [content]
  );
  const paragraphs = useMemo(() => (content || "").split(/\n\n+/), [content]);
  const parsed = useMemo(() => {
    if (!hasSpeakerLabels) return [];
    const labelPattern = /^\[Speaker (\d+)\]:\s*([\s\S]*)$/;
    return paragraphs.map((para) => {
      const labelMatch = labelPattern.exec(para);
      if (!labelMatch) {
        return { type: "text" as const, text: para };
      }
      return {
        type: "speaker" as const,
        speakerNum: parseInt(labelMatch[1], 10),
        text: labelMatch[2],
      };
    });
  }, [hasSpeakerLabels, paragraphs]);

  if (!hasSpeakerLabels) {
    return <span>{content}</span>;
  }

  return (
    <div className="space-y-4">
      {parsed.map((segment, idx) => {
        if (segment.type === "speaker") {
          const colorIdx = (segment.speakerNum - 1) % SPEAKER_COLORS.length;
          const colors = SPEAKER_COLORS[colorIdx];

          return (
            <div key={idx} className="flex flex-col gap-1">
              <span
                className={`inline-flex items-center self-start px-2.5 py-0.5 rounded-full text-xs font-medium border ${colors.bg} ${colors.text} ${colors.border}`}
              >
                {t("Speaker {{number}}", { number: formatNumber(segment.speakerNum) })}
              </span>
              <p className="leading-relaxed">{segment.text}</p>
            </div>
          );
        }
        return <p key={idx} className="leading-relaxed">{segment.text}</p>;
      })}
    </div>
  );
}

// FitText component that dynamically scales font size to fit container
interface FitTextProps {
  children: string;
  minFontSize?: number;
  maxFontSize?: number;
  className?: string;
}

function FitText({ children, minFontSize = 12, maxFontSize = 24, className = "" }: FitTextProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const measureRef = useRef<HTMLSpanElement>(null);
  const [fontSize, setFontSize] = useState(maxFontSize);

  const calculateFit = useCallback(() => {
    const container = containerRef.current;
    const measureSpan = measureRef.current;
    if (!container || !measureSpan) return;

    const containerWidth = container.offsetWidth;
    if (containerWidth === 0) return;

    measureSpan.style.fontSize = `${maxFontSize}px`;
    const textWidth = measureSpan.offsetWidth;

    if (textWidth > containerWidth) {
      // Calculate the scale factor and apply it
      const scale = containerWidth / textWidth;
      const newSize = Math.max(minFontSize, Math.floor(maxFontSize * scale));
      setFontSize(newSize);
    } else {
      setFontSize(maxFontSize);
    }
  }, [children, maxFontSize, minFontSize]);

  // Calculate on mount and when children change
  useLayoutEffect(() => {
    // Small delay to ensure container is rendered
    const timer = requestAnimationFrame(() => {
      calculateFit();
    });
    return () => cancelAnimationFrame(timer);
  }, [children, calculateFit]);

  // Recalculate on resize
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const resizeObserver = new ResizeObserver(() => {
      calculateFit();
    });

    resizeObserver.observe(container);
    return () => resizeObserver.disconnect();
  }, [calculateFit]);

  return (
    <div ref={containerRef} className="w-full relative">
      <span
        ref={measureRef}
        aria-hidden="true"
        className="pointer-events-none absolute opacity-0 whitespace-nowrap font-bold tracking-tight"
        style={{ fontSize: `${maxFontSize}px` }}
      >
        {children}
      </span>
      <span
        className={className}
        style={{
          fontSize: `${fontSize}px`,
          display: 'block',
          whiteSpace: 'nowrap',
          overflow: 'hidden',
          textOverflow: 'ellipsis'
        }}
        title={children}
      >
        {children}
      </span>
    </div>
  );
}

function formatElapsedDuration(seconds: number): string {
  const hours = Math.floor(seconds / 3600);
  const mins = Math.floor((seconds % 3600) / 60);
  const secs = seconds % 60;
  if (hours > 0) {
    return `${hours.toString().padStart(2, "0")}:${mins.toString().padStart(2, "0")}:${secs.toString().padStart(2, "0")}`;
  }
  return `${mins.toString().padStart(2, "0")}:${secs.toString().padStart(2, "0")}`;
}

function useTranscriptDuration({
  status,
  duration,
  startedAt,
  resetKey,
}: {
  status?: string;
  duration?: string;
  startedAt?: string;
  resetKey?: string;
}): string {
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const fallbackStartedAtRef = useRef<number | null>(null);
  const activeKeyRef = useRef("");

  const parsedStartMs = useMemo(() => {
    const raw = (startedAt || "").trim();
    if (!raw) return null;
    const normalized = raw.includes("T") ? raw : raw.replace(" ", "T");
    const millis = Date.parse(normalized);
    return Number.isFinite(millis) ? millis : null;
  }, [startedAt]);

  useEffect(() => {
    if (status !== "processing") {
      fallbackStartedAtRef.current = null;
      activeKeyRef.current = "";
      setElapsedSeconds(0);
      return;
    }

    if (activeKeyRef.current !== (resetKey || "")) {
      activeKeyRef.current = resetKey || "";
      fallbackStartedAtRef.current = null;
    }
    if (parsedStartMs === null && fallbackStartedAtRef.current === null) {
      fallbackStartedAtRef.current = Date.now();
    }

    const updateElapsed = () => {
      const base = parsedStartMs ?? fallbackStartedAtRef.current;
      if (base === null) {
        setElapsedSeconds(0);
        return;
      }
      const elapsed = Math.max(0, Math.floor((Date.now() - base) / 1000));
      setElapsedSeconds((current) => current === elapsed ? current : elapsed);
    };

    updateElapsed();
    const interval = window.setInterval(updateElapsed, 1000);
    return () => window.clearInterval(interval);
  }, [parsedStartMs, resetKey, status]);

  return status === "processing" ? formatElapsedDuration(elapsedSeconds) : (duration || "");
}

function StopButton({ transcriptId, onStop }: { transcriptId: string; onStop: () => void }) {
  const [isStopping, setIsStopping] = useState(false);
  const { toast } = useToast();
  const { t } = useI18n();

  const handleStop = async () => {
    if (isStopping) return;
    setIsStopping(true);
    try {
      const res = await fetchWithTimeout(apiUrl(`/api/transcripts/${transcriptId}/cancel`), {
        method: "POST",
        credentials: "include",
      }, 15_000);
      if (!res.ok) throw new Error(t("Failed to stop"));

      toast({ title: t("Stopping…"), description: t("Task cancellation requested.") });
      onStop();
    } catch {
      toast({ title: t("Error"), description: t("Failed to stop task."), variant: "destructive" });
      setIsStopping(false);
    }
  };

  return (
    <Button size="sm" variant="destructive" onClick={handleStop} disabled={isStopping} type="button">
      {isStopping ? <Loader2 className="w-3 h-3 mr-1 animate-spin" /> : <Square className="w-3 h-3 mr-1 fill-current" />}
      {t("Stop")}
    </Button>
  );
}

function SummarizeButton({
  transcriptId,
  onComplete,
  disabled = false,
  label = "Summarize",
}: {
  transcriptId: string | undefined;
  onComplete: () => void;
  disabled?: boolean;
  label?: string;
}) {
  const [isSummarizing, setIsSummarizing] = useState(false);
  const { toast } = useToast();
  const { t } = useI18n();

  const handleSummarize = async () => {
    if (!transcriptId || isSummarizing || disabled) return;
    setIsSummarizing(true);
    try {
      const res = await fetchWithTimeout(apiUrl(`/api/transcripts/${transcriptId}/summarize`), {
        method: "POST",
        credentials: "include",
      }, 15 * 60_000);
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.message || res.statusText);
      }
      toast({
        title: t("Summary generated"),
        description: t("The transcript has been summarized."),
        duration: 3000,
      });
    } catch (e: any) {
      toast({
        title: t("Summarization failed"),
        description: t(String(e?.message || e)),
        duration: 4000,
      });
    } finally {
      onComplete();
      setIsSummarizing(false);
    }
  };

  return (
    <Button size="sm" variant="outline" onClick={handleSummarize} disabled={isSummarizing || disabled} type="button">
      {isSummarizing ? <Loader2 className="w-3 h-3 mr-1 animate-spin" /> : <Sparkles className="w-3 h-3 mr-1" />}
      {isSummarizing ? t("Summarizing…") : t(label)}
    </Button>
  );
}

export default function TranscriptDetail() {
  const { id } = useParams();
  const [, setLocation] = useLocation();
  const { toast } = useToast();
  const { formatDate, formatLegacyDate, formatNumber, t } = useI18n();
  const [copied, setCopied] = useState(false);
  const [copiedSummary, setCopiedSummary] = useState(false);
  const copyResetTimerRef = useRef<number | null>(null);
  const summaryCopyResetTimerRef = useRef<number | null>(null);
  const mainScrollRef = useRef<HTMLElement | null>(null);
  const [isRetryingYoutube, setIsRetryingYoutube] = useState(false);
  const queryClient = useQueryClient();
  const { isWsConnected } = useTranscriptAutoRefresh({
    transcriptId: id,
  });

  const transcriptQuery = useQuery<TranscriptDetailResponse>({
    queryKey: ["/api/transcripts", id],
    enabled: !!id,
    staleTime: 0,
    refetchIntervalInBackground: true,
    refetchInterval: (query) => {
      const data = query.state.data;
      const status = data?.status;
      const step = String(data?.step || "").toLowerCase();
      const summary = String(data?.summary || "").trim();
      const summaryStatus = String(data?.summaryStatus || "").toLowerCase();
      const type = String(data?.type || "");
      const isActive = status === "processing" || status === "recording";
      if (isActive) {
        // Keep a light polling fallback even with WS connected in case
        // a WS event is missed or delayed.
        return isWsConnected ? 3000 : 1000;
      }
      const updatedAtMs = Date.parse(String(data?.updatedAt || ""));
      const isRecentlyUpdated = Number.isFinite(updatedAtMs)
        ? Date.now() - updatedAtMs < 5 * 60 * 1000
        : true;
      const settings = queryClient.getQueryData<SettingsResponse>(["/api/settings"]);
      const mayAutoSummarize = type !== "mic" && settings?.autoSummarize !== false;
      const isSummaryPending =
        status === "completed" &&
        !summary &&
        summaryStatus !== "failed" &&
        (summaryStatus === "pending" || step.includes("summariz") || mayAutoSummarize) &&
        isRecentlyUpdated;
      if (isSummaryPending) {
        // Completed transcripts can still be in summarization.
        // Poll lightly so summary appears even if a WS update was missed.
        return isWsConnected ? 2500 : 1200;
      }
      // If no data yet (or temporary fetch failure), keep retrying.
      if (!data) {
        return 1500;
      }
      return false;
    },
  });
  const transcriptData = transcriptQuery.data;
  const needsAutoSummarySetting = Boolean(
    transcriptData
      && transcriptData.type !== "mic"
      && transcriptData.status === "completed"
      && !String(transcriptData.summary || "").trim()
      && String(transcriptData.summaryStatus || "").toLowerCase() !== "failed",
  );
  // Auto-summary settings only affect completed file/YouTube jobs without a
  // summary. Mic details and active jobs no longer pay for an unrelated fetch.
  useQuery<SettingsResponse>({
    queryKey: ["/api/settings"],
    enabled: needsAutoSummarySetting,
    staleTime: 5 * 60_000,
    queryFn: async ({ signal }) => {
      const res = await fetchWithTimeout(apiUrl("/api/settings"), {
        credentials: "include",
        cache: "no-store",
        signal,
      }, 10_000);
      if (!res.ok) return {};
      return (await res.json()) as SettingsResponse;
    },
  });
  const transcript: TranscriptDetailResponse = transcriptQuery.data || {
    id: id || "",
    title: t("Transcript"),
    date: "",
    duration: "",
    status: "completed",
    content: "",
    type: "mic",
  };
  const durationDisplay = useTranscriptDuration({
    status: transcript.status,
    duration: transcript.duration,
    startedAt: transcript.processingStartedAt,
    resetKey: transcript.id,
  });
  const dateDisplay = transcript.createdAt
    ? formatDate(transcript.createdAt, { dateStyle: "medium", timeStyle: "short" })
    : formatLegacyDate(transcript.date);
  const summarySource = useMemo(
    () => String(transcript?.summary || "").trim(),
    [transcript?.summary],
  );
  const summaryFormat = transcript.summaryFormat === "html" ? "html" : "markdown";
  const preparedSummaryHtml = useMemo(
    () => summaryFormat === "html"
      ? prepareSummaryHtml(summarySource)
      : { html: "", outline: [], plainText: "" },
    [summaryFormat, summarySource],
  );
  const summaryMarkdown = useMemo(
    () => normalizeSummaryMarkdown(summarySource),
    [summarySource],
  );
  const summaryCopyText = summaryFormat === "html" ? preparedSummaryHtml.plainText : summaryMarkdown;
  const hasSummary = summarySource.length > 0;
  const summaryStatus = String(transcript?.summaryStatus || (hasSummary ? "completed" : "idle")).toLowerCase();
  const summaryStepLower = String(transcript?.step || "").toLowerCase();
  const updatedAtMs = Date.parse(String(transcript?.updatedAt || ""));
  const isSummaryStep = summaryStepLower.includes("summariz");
  const isSummaryStepFresh = Number.isFinite(updatedAtMs) ? Date.now() - updatedAtMs < 3 * 60 * 1000 : true;
  const isSummaryInProgress = !hasSummary && (summaryStatus === "pending" || (isSummaryStep && isSummaryStepFresh));
  const isSummaryStepStale = !hasSummary && summaryStatus !== "failed" && isSummaryStep && !isSummaryStepFresh;
  const isSummaryFailed = !hasSummary && summaryStatus === "failed";
  const summaryFailureMessage = friendlyRequestMessage(
    String(transcript?.summaryError || "").trim(),
    t("Summary generation failed."),
  );
  const summaryActionLabel = isSummaryFailed ? "Retry Summary" : "Summarize";
  const showHeaderSummaryAction =
    transcript.status === "completed" && !hasSummary && !isSummaryInProgress && !isSummaryFailed;
  const isFailedYoutubeTranscript =
    transcript?.status === "failed" && transcript?.type === "youtube";
  const rawFailureMessage = useMemo(
    () => extractFailureMessage(String(transcript?.content || ""), String(transcript?.step || "")),
    [transcript?.content, transcript?.step],
  );
  const failedMessage = useMemo(
    () => (isFailedYoutubeTranscript ? t(friendlyRequestMessage(rawFailureMessage, t("Transcription failed."))) : ""),
    [isFailedYoutubeTranscript, rawFailureMessage, t],
  );
  const technicalFailureMessage = useMemo(() => {
    if (!isFailedYoutubeTranscript) return "";
    const technical = (rawFailureMessage || "").trim();
    if (!technical || technical === failedMessage) return "";
    return technical;
  }, [failedMessage, isFailedYoutubeTranscript, rawFailureMessage]);
  const failedContentLooksLikeErrorOnly = useMemo(() => {
    if (!isFailedYoutubeTranscript) return false;
    const content = String(transcript?.content || "").trim();
    return /^\[(error|timeout|download error)\]/i.test(content);
  }, [isFailedYoutubeTranscript, transcript?.content]);

  const retryYoutubeTranscription = useCallback(async () => {
    if (!id || isRetryingYoutube) return;
    const sourceUrl = String(transcript?.sourceUrl || "").trim();
    if (!sourceUrl) {
      toast({
        title: t("Retry unavailable"),
        description: t("No source URL is available for this transcript."),
        variant: "destructive",
      });
      return;
    }

    setIsRetryingYoutube(true);
    try {
      const res = await fetchWithTimeout(apiUrl("/api/youtube/transcribe"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({
          url: sourceUrl,
          title: transcript?.title,
          channelTitle: transcript?.channel,
          thumbnailUrl: transcript?.thumbnailUrl,
          duration: transcript?.duration,
        }),
      }, 15_000);
      if (!res.ok) {
        throw new Error(await responseErrorMessage(res));
      }

      const rec = (await res.json()) as TranscriptHistoryItem;
      if (!rec?.id) {
        throw new Error(t("Retry started, but no transcript ID was returned."));
      }

      toast({
        title: t("Retry started"),
        description: t("A new YouTube transcription attempt has been queued."),
        duration: 3000,
      });
      queryClient.invalidateQueries({ queryKey: ["/api/transcripts"] });
      setLocation(`/transcript/${rec.id}`);
    } catch (e) {
      toast({
        title: t("Retry failed"),
        description: t(friendlyError(e, t("Could not restart transcription."))),
        variant: "destructive",
        duration: 5000,
      });
    } finally {
      setIsRetryingYoutube(false);
    }
  }, [id, isRetryingYoutube, queryClient, setLocation, t, toast, transcript?.sourceUrl, transcript?.title, transcript?.channel, transcript?.thumbnailUrl, transcript?.duration]);

  useEffect(() => () => {
    if (copyResetTimerRef.current !== null) {
      window.clearTimeout(copyResetTimerRef.current);
    }
    if (summaryCopyResetTimerRef.current !== null) {
      window.clearTimeout(summaryCopyResetTimerRef.current);
    }
  }, []);

  const handleCopyTranscript = useCallback(async () => {
    try {
      if (!navigator.clipboard?.writeText) {
        throw new Error(t("Clipboard API unavailable"));
      }
      await navigator.clipboard.writeText(transcript?.content || "");
      setCopied(true);
      if (copyResetTimerRef.current !== null) {
        window.clearTimeout(copyResetTimerRef.current);
      }
      copyResetTimerRef.current = window.setTimeout(() => setCopied(false), 2000);
      toast({
        title: t("Copied to Clipboard"),
        description: t("Transcript content has been copied."),
        duration: 2000,
      });
    } catch {
      setCopied(false);
      toast({
        title: t("Copy failed"),
        description: t("Scriber could not access the clipboard."),
        variant: "destructive",
      });
    }
  }, [t, toast, transcript?.content]);

  const handleCopySummary = useCallback(async () => {
    try {
      if (!navigator.clipboard?.writeText) {
        throw new Error(t("Clipboard API unavailable"));
      }
      await navigator.clipboard.writeText(summaryCopyText || "");
      setCopiedSummary(true);
      if (summaryCopyResetTimerRef.current !== null) {
        window.clearTimeout(summaryCopyResetTimerRef.current);
      }
      summaryCopyResetTimerRef.current = window.setTimeout(() => setCopiedSummary(false), 2000);
      toast({
        title: t("Copied to Clipboard"),
        description: t("Summary has been copied."),
        duration: 2000,
      });
    } catch {
      setCopiedSummary(false);
      toast({
        title: t("Copy failed"),
        description: t("Scriber could not access the clipboard."),
        variant: "destructive",
      });
    }
  }, [summaryCopyText, t, toast]);

  const getBackLink = () => {
    switch (transcript?.type) {
      case "youtube":
        return "/youtube";
      case "file":
        return "/file";
      default:
        return "/";
    }
  };

  // Controlled accordion state - reacts when summary becomes available
  const [accordionValue, setAccordionValue] = useState<string[]>(
    transcript.summary ? ["summary"] : ["transcript"]
  );

  // Update accordion when summary becomes available
  useEffect(() => {
    if (transcript.summary) {
      setAccordionValue(["summary"]);
    }
  }, [transcript.summary]);
  const showSummaryToc = summaryFormat === "html"
    && preparedSummaryHtml.outline.length >= 2
    && accordionValue.includes("summary");

  return (
    <div className="h-screen bg-background flex flex-col overflow-hidden">
      <DesktopTitleBar />
      {/* Header Toolbar */}
      <header className="z-40 shrink-0 backdrop-blur-md border-b border-border/50 h-16 flex items-center justify-between px-4 md:px-8 gap-4" style={{ background: 'var(--neu-bg)' }}>
        <div className="flex items-center gap-4 min-w-0 flex-1">
          <Link href={getBackLink()}>
            <Button variant="ghost" size="icon" className="-ml-2 shrink-0" aria-label={t("Go back")}>
              <ArrowLeft className="w-5 h-5 text-muted-foreground" />
            </Button>
          </Link>
          <div className="min-w-0 flex-1">
            <FitText className="font-bold tracking-tight text-foreground" minFontSize={14} maxFontSize={24}>
              {transcript?.title || t("Transcript")}
            </FitText>
            <p className="text-xs text-muted-foreground truncate">
              {dateDisplay} • <span>{durationDisplay}</span>
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2 shrink-0">
          {transcript.content && (
            <Button
              variant={copied ? "default" : "outline"}
              size="sm"
              className="hidden md:flex"
              onClick={handleCopyTranscript}
              type="button"
            >
              {copied ? <Check className="w-4 h-4 mr-2" /> : <Copy className="w-4 h-4 mr-2" />}
              {copied ? t("Copied!") : t("Copy transcript")}
            </Button>
          )}
          {transcript.summary && (
            <Button
              variant={copiedSummary ? "default" : "outline"}
              size="sm"
              className="hidden md:flex"
              onClick={handleCopySummary}
              type="button"
            >
              {copiedSummary ? <Check className="w-4 h-4 mr-2" /> : <Copy className="w-4 h-4 mr-2" />}
              {copiedSummary ? t("Copied!") : t("Copy summary")}
            </Button>
          )}
          <DropdownMenu modal={false}>
            <DropdownMenuTrigger asChild>
              <Button variant="outline" size="sm" className="hidden md:flex data-[state=open]:bg-accent" style={{ transform: 'none' }} type="button">
                <Download className="w-4 h-4 mr-2" /> {t("Export")}
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem
                onClick={() => {
                  window.open(apiUrl(`/api/transcripts/${id}/export/pdf`), '_blank');
                }}
              >
                <FileText className="w-4 h-4 mr-2" /> {t("Export as PDF")}
              </DropdownMenuItem>
              <DropdownMenuItem
                onClick={() => {
                  window.open(apiUrl(`/api/transcripts/${id}/export/docx`), '_blank');
                }}
              >
                <FileText className="w-4 h-4 mr-2" /> {t("Export as DOCX")}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>

          <DropdownMenu modal={false}>
            <DropdownMenuTrigger asChild>
              <Button variant="outline" size="icon" className="md:hidden" aria-label={t("Open transcript actions")} type="button">
                <Download className="w-4 h-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              {transcript.content && (
                <DropdownMenuItem onClick={handleCopyTranscript}>
                  <Copy className="w-4 h-4 mr-2" /> {t("Copy transcript")}
                </DropdownMenuItem>
              )}
              {transcript.summary && (
                <DropdownMenuItem onClick={handleCopySummary}>
                  <Copy className="w-4 h-4 mr-2" /> {t("Copy summary")}
                </DropdownMenuItem>
              )}
              <DropdownMenuItem
                onClick={() => {
                  window.open(apiUrl(`/api/transcripts/${id}/export/pdf`), "_blank");
                }}
              >
                <FileText className="w-4 h-4 mr-2" /> {t("Export as PDF")}
              </DropdownMenuItem>
              <DropdownMenuItem
                onClick={() => {
                  window.open(apiUrl(`/api/transcripts/${id}/export/docx`), "_blank");
                }}
              >
                <FileText className="w-4 h-4 mr-2" /> {t("Export as DOCX")}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          {transcript.status === "processing" && (
            <StopButton transcriptId={id!} onStop={() => queryClient.invalidateQueries({ queryKey: ["/api/transcripts", id] })} />
          )}
          {showHeaderSummaryAction && (
            <div className="hidden md:block">
              <SummarizeButton transcriptId={id} label={summaryActionLabel} onComplete={() => queryClient.invalidateQueries({ queryKey: ["/api/transcripts", id] })} />
            </div>
          )}
          {showHeaderSummaryAction && (
            <div className="md:hidden">
              <SummarizeButton transcriptId={id} label={summaryActionLabel} onComplete={() => queryClient.invalidateQueries({ queryKey: ["/api/transcripts", id] })} />
            </div>
          )}
        </div>
      </header>

      {/* Main Content */}
      <main ref={mainScrollRef} className="transcript-detail-scroll min-h-0 flex-1 overflow-y-auto p-4 md:p-8 md:px-12 lg:px-8 xl:px-12">
        <div className={`transcript-detail-shell space-y-6${showSummaryToc ? " has-summary-toc" : ""}`}>

          {/* Meta Card */}
          <div className="flex flex-wrap gap-2">
            <Badge variant="secondary" className="px-3 py-1 font-normal neu-button"><Calendar className="w-3 h-3 mr-1.5" /> {dateDisplay}</Badge>
          </div>

          {transcriptQuery.isError && (
            <QueryErrorState
              title={t("Could not load transcript")}
              description={t("Please retry loading this transcript.")}
              onRetry={() => transcriptQuery.refetch()}
            />
          )}

          {/* Processing Status Banner */}
          {transcript.status === "processing" && (
            <div className="neu-status-well p-4 flex items-center gap-3">
              <Loader2 className="w-5 h-5 animate-spin text-primary shrink-0" />
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-foreground truncate">
                  {localizedProcessingStep(transcript.step, "Processing…", t, formatNumber)}
                </p>
                <p className="text-xs text-muted-foreground">
                  {t("Elapsed")}: <span>{durationDisplay}</span>
                </p>
              </div>
            </div>
          )}

          {isFailedYoutubeTranscript && (
            <div className="space-y-2">
              <QueryErrorState
                title={t("YouTube transcription failed")}
                description={failedMessage || t("The transcription failed. Please try again.")}
                onRetry={() => {
                  void retryYoutubeTranscription();
                }}
              />
              {technicalFailureMessage && (
                <p className="text-xs text-muted-foreground px-1">
                  {t("Technical details")}: {technicalFailureMessage}
                </p>
              )}
            </div>
          )}

          {isSummaryFailed && (
            <div className="space-y-3">
              <QueryErrorState
                title={t("Summary generation failed")}
                description={t(summaryFailureMessage)}
              />
              <SummarizeButton
                transcriptId={id}
                label={t("Retry Summary")}
                onComplete={() => queryClient.invalidateQueries({ queryKey: ["/api/transcripts", id] })}
              />
            </div>
          )}

          <div className="transcript-summary-layout">
            {showSummaryToc && (
              <SummaryTableOfContents outline={preparedSummaryHtml.outline} scrollContainerRef={mainScrollRef} />
            )}
            {/* Accordion with Summary and Transcript */}
            <Accordion type="multiple" value={accordionValue} onValueChange={setAccordionValue} className="transcript-detail-accordion space-y-4">
            {/* Summary Section */}
            <AccordionItem value="summary" className="neu-recording-row overflow-hidden">
              <AccordionTrigger className="px-4 py-3 hover:no-underline">
                <div className="flex items-center gap-2">
                  <Sparkles className="w-4 h-4 text-primary" />
                  <span className="text-base font-semibold tracking-tight">{t("Summary")}</span>
                  {isSummaryInProgress && (
                    <span className="flex items-center gap-1 text-xs text-muted-foreground ml-2">
                      <Loader2 className="w-3 h-3 animate-spin" />
                      {localizedProcessingStep(transcript.step, "Summarizing…", t, formatNumber)}
                    </span>
                  )}
                  {isSummaryStepStale && (
                    <span className="text-xs text-amber-600 ml-2">{t("Summarization timed out")}</span>
                  )}
                  {isSummaryFailed && (
                    <span className="text-xs text-destructive ml-2">{t("Summarization failed")}</span>
                  )}
                </div>
              </AccordionTrigger>
              <AccordionContent className="px-4 pb-4">
                {hasSummary ? (
                  summaryFormat === "html" ? (
                    <TranscriptSummaryDocument prepared={preparedSummaryHtml} />
                  ) : (
                    <div className="summary-document summary-document--markdown">
                      <ReactMarkdown>{summaryMarkdown}</ReactMarkdown>
                    </div>
                  )
                ) : (
                  <p className="text-base text-muted-foreground italic">
                    {transcript.status === "completed"
                      ? isSummaryInProgress
                        ? t("Summary is currently being generated.")
                        : isSummaryFailed
                          ? t("{{message}} Use “Retry Summary” to try again.", { message: t(summaryFailureMessage) })
                        : isSummaryStepStale
                          ? t("Summary generation timed out. Select “Summarize” in the header to retry.")
                          : t("No summary yet. Select “Summarize” in the header to generate one.")
                      : t("Summary will be available after transcription completes.")}
                  </p>
                )}
              </AccordionContent>
            </AccordionItem>

            {/* Transcript Section */}
            <AccordionItem value="transcript" className="neu-recording-row overflow-hidden">
              <AccordionTrigger className="px-4 py-3 hover:no-underline">
                <div className="flex items-center gap-2">
                  <FileText className="w-4 h-4 text-blue-600" />
                  <span className="text-base font-semibold tracking-tight">{t("Transcript")}</span>
                </div>
              </AccordionTrigger>
              <AccordionContent className="px-4 pb-4">
                <div className="transcript-content">
                  {transcriptQuery.isLoading ? (
                    t("Loading…")
                  ) : transcript.status === "processing" ? (
                    <span className="text-muted-foreground italic"></span>
                  ) : isFailedYoutubeTranscript && failedContentLooksLikeErrorOnly ? (
                    <span className="text-muted-foreground italic">
                      {failedMessage || t("No transcript text captured.")}
                    </span>
                  ) : transcript.content ? (
                    <SpeakerFormattedText content={transcript.content} />
                  ) : (
                    t("No transcript text captured.")
                  )}
                </div>
              </AccordionContent>
            </AccordionItem>
          </Accordion>
          </div>

        </div>
      </main>
    </div>
  );
}

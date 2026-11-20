import { useParams, Link, useLocation } from "wouter";
import { TranscriptionDiarizationNotice } from "@/components/transcription-diarization-notice";
import { TranscriptionBoundaryNotice } from "@/components/transcription-boundary-notice";
import { FileTranscriptResumeButton } from "@/components/file-transcript-resume-button";
import {
  ArrowLeft,
  Download,
  Copy,
  Check,
  ChevronDown,
  ChevronUp,
  Sparkles,
  FileText,
  ScrollText,
  Search,
  X,
  Youtube,
  ExternalLink,
} from "lucide-react";
import { WavePhysicsLoader } from "@/components/ui/wave-physics-loader";
import { Button } from "@/components/ui/button";
import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from "@/components/ui/accordion";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useToast } from "@/hooks/use-toast";
import {
  memo,
  useState,
  useEffect,
  useRef,
  useCallback,
  useDeferredValue,
  useMemo,
  type ReactNode,
  type RefObject,
} from "react";
import { apiUrl, isTauriRuntime } from "@/lib/backend";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";
import ReactMarkdown from "react-markdown";
import { QueryErrorState } from "@/components/ui/query-error-state";
import { YouTubeSessionSettings } from "@/components/settings/YouTubeSessionSettings";
import { requiresYouTubeSignIn } from "@/lib/request-errors";
import { SummaryTableOfContents, TranscriptSummaryDocument } from "@/components/transcript-summary-document";
import { TranscriptStopButton } from "@/components/transcript-stop-button";
import { FileTranscriptRetryButton } from "@/components/file-transcript-retry-button";
import { useAppScrollContainerRef } from "@/contexts/AppScrollContainerContext";
import { useTranscriptAutoRefresh } from "@/hooks/use-transcript-auto-refresh";
import { useIsMobile } from "@/hooks/use-mobile";
import {
  extractFailureMessage,
  friendlyError,
  friendlyRequestMessage,
  isFailureOnlyContent,
  responseErrorMessage,
} from "@/lib/request-errors";
import { prepareSummaryHtml } from "@/lib/summary-html";
import { summaryTableOfContentsTitle } from "@/lib/summary-document-language";
import { saveTranscriptExport, saveTranscriptPdfExport } from "@/lib/transcript-export";
import { markPodcastTranscriptRead } from "@/lib/podcast-progress";
import {
  cycleMatchIndex,
  findPreparedTranscriptMatches,
  genericSpeakerNumber,
  parseTranscriptContent,
  prepareTranscriptSearchIndex,
  speakerColorIndex,
  type TranscriptBlock,
  type TranscriptMatch,
} from "@/lib/transcript-content";
import type { SettingsResponse, TranscriptDetailResponse, TranscriptHistoryItem } from "@/lib/api-types";
import { useI18n } from "@/i18n";

const MemoizedMarkdown = memo(ReactMarkdown);

function normalizeSummaryMarkdown(text: string): string {
  return (text || "")
    .replace(/\r\n?/g, "\n")
    .replace(/\u00a0/g, " ")
    .replace(/\u200b/g, "")
    .replace(/^([ \t]*)[•●◦▪▫‣⁃]\s+/gm, "$1- ")
    .trim();
}

function normalizeYoutubeSourceUrl(value: string | null | undefined): string {
  const rawValue = String(value || "").trim();
  if (!rawValue) return "";

  let parsed: URL;
  try {
    parsed = new URL(rawValue);
  } catch {
    try {
      parsed = new URL(`https://${rawValue}`);
    } catch {
      return "";
    }
  }

  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") return "";
  const hostname = parsed.hostname.toLowerCase().replace(/\.$/, "");
  const trustedHost =
    hostname === "youtu.be" ||
    hostname === "youtube.com" ||
    hostname.endsWith(".youtube.com") ||
    hostname === "youtube-nocookie.com" ||
    hostname.endsWith(".youtube-nocookie.com");
  return trustedHost ? parsed.toString() : "";
}

async function openYoutubeSourceUrl(url: string): Promise<void> {
  try {
    const { openUrl } = await import("@tauri-apps/plugin-opener");
    await openUrl(url);
  } catch {
    window.open(url, "_blank", "noopener,noreferrer");
  }
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
  {
    bg: "bg-blue-100 dark:bg-blue-900/40",
    text: "text-blue-700 dark:text-blue-300",
    border: "border-blue-300 dark:border-blue-700",
  },
  {
    bg: "bg-emerald-100 dark:bg-emerald-900/40",
    text: "text-emerald-700 dark:text-emerald-300",
    border: "border-emerald-300 dark:border-emerald-700",
  },
  {
    bg: "bg-amber-100 dark:bg-amber-900/40",
    text: "text-amber-700 dark:text-amber-300",
    border: "border-amber-300 dark:border-amber-700",
  },
  {
    bg: "bg-purple-100 dark:bg-purple-900/40",
    text: "text-purple-700 dark:text-purple-300",
    border: "border-purple-300 dark:border-purple-700",
  },
  {
    bg: "bg-rose-100 dark:bg-rose-900/40",
    text: "text-rose-700 dark:text-rose-300",
    border: "border-rose-300 dark:border-rose-700",
  },
  {
    bg: "bg-cyan-100 dark:bg-cyan-900/40",
    text: "text-cyan-700 dark:text-cyan-300",
    border: "border-cyan-300 dark:border-cyan-700",
  },
];

const EMPTY_MATCHES: readonly TranscriptMatch[] = [];

interface BlockMatches {
  /** Global index of this block's first match. */
  offset: number;
  matches: TranscriptMatch[];
}

function highlightedText(text: string, matches: readonly TranscriptMatch[], activeMatch: number): ReactNode {
  if (!matches.length) return text;
  const parts: ReactNode[] = [];
  let cursor = 0;
  matches.forEach((match, index) => {
    if (match.start > cursor) parts.push(text.slice(cursor, match.start));
    const active = index === activeMatch;
    parts.push(
      <mark
        key={`${match.start}-${match.end}`}
        className="transcript-find-match"
        data-active={active ? "true" : undefined}
        aria-current={active ? "true" : undefined}
      >
        {text.slice(match.start, match.end)}
      </mark>,
    );
    cursor = match.end;
  });
  if (cursor < text.length) parts.push(text.slice(cursor));
  return parts;
}

// One parsed transcript block: an optional quiet timestamp/speaker row above
// text whose explicit line breaks are preserved.
const TranscriptBlockView = memo(function TranscriptBlockView({
  block,
  matches,
  activeMatch,
}: {
  block: TranscriptBlock;
  matches: readonly TranscriptMatch[];
  activeMatch: number;
}) {
  const { formatNumber, t } = useI18n();
  const speakerNumber = genericSpeakerNumber(block.speaker);
  const speakerLabel =
    block.speaker === undefined || block.continued
      ? ""
      : speakerNumber !== null
        ? t("Speaker {{number}}", { number: formatNumber(speakerNumber) })
        : block.speaker;
  const colors = SPEAKER_COLORS[speakerColorIndex(block.speaker, SPEAKER_COLORS.length)];
  const hasHeader = Boolean(block.startLabel || speakerLabel);
  return (
    <div className="flex flex-col gap-1" data-transcript-block="">
      {hasHeader && (
        <div className="flex flex-wrap items-center gap-2 text-xs" data-transcript-block-header="">
          {block.startLabel && (
            <span className="font-mono tabular-nums text-muted-foreground" data-transcript-timestamp="">
              {block.startLabel}
            </span>
          )}
          {speakerLabel && (
            <span
              className={`inline-flex items-center rounded-full border px-2.5 py-0.5 font-medium ${colors.bg} ${colors.text} ${colors.border}`}
              data-transcript-speaker=""
            >
              {speakerLabel}
            </span>
          )}
        </div>
      )}
      {block.text && (
        <p className="whitespace-pre-wrap break-words leading-relaxed">
          {highlightedText(block.text, matches, activeMatch)}
        </p>
      )}
    </div>
  );
});

const TranscriptBlocksView = memo(function TranscriptBlocksView({
  blocks,
  matchesByBlock,
  activeMatch,
}: {
  blocks: readonly TranscriptBlock[];
  matchesByBlock: ReadonlyMap<number, BlockMatches>;
  activeMatch: number;
}) {
  return (
    <div className="space-y-4">
      {blocks.map((block, index) => {
        const entry = matchesByBlock.get(index);
        const localActive =
          entry && activeMatch >= entry.offset && activeMatch < entry.offset + entry.matches.length
            ? activeMatch - entry.offset
            : -1;
        return (
          <TranscriptBlockView
            key={index}
            block={block}
            matches={entry?.matches ?? EMPTY_MATCHES}
            activeMatch={localActive}
          />
        );
      })}
    </div>
  );
});

function prefersReducedMotion(): boolean {
  try {
    return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true;
  } catch {
    return false;
  }
}

function findShortcutLabel(t: ReturnType<typeof useI18n>["t"]): string {
  const platform = typeof navigator === "undefined" ? "" : String(navigator.platform || navigator.userAgent || "");
  return /Mac|iPhone|iPad/i.test(platform) ? "⌘F" : t("Ctrl+F");
}

function TranscriptFindBar({
  inputRef,
  query,
  total,
  activeMatch,
  onQueryChange,
  onStep,
  onClose,
}: {
  inputRef: RefObject<HTMLInputElement | null>;
  query: string;
  total: number;
  activeMatch: number;
  onQueryChange: (value: string) => void;
  onStep: (direction: 1 | -1) => void;
  onClose: () => void;
}) {
  const { formatNumber, t } = useI18n();
  const hasQuery = query.trim().length > 0;
  const counter = hasQuery ? `${formatNumber(total > 0 ? activeMatch + 1 : 0)} / ${formatNumber(total)}` : "";
  return (
    <div
      role="search"
      aria-label={t("Find in transcript")}
      className="ml-auto flex h-9 items-center gap-0.5 rounded-full border border-border/70 bg-background/80 pl-3 pr-1 shadow-sm"
      data-transcript-find-bar=""
    >
      <Search className="h-3.5 w-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
      <input
        ref={inputRef}
        type="search"
        value={query}
        onChange={(event) => onQueryChange(event.currentTarget.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            onStep(event.shiftKey ? -1 : 1);
          } else if (event.key === "Escape") {
            event.preventDefault();
            event.stopPropagation();
            onClose();
          }
        }}
        placeholder={t("Search transcript")}
        aria-label={t("Search transcript")}
        autoComplete="off"
        spellCheck={false}
        className="h-8 w-28 min-w-0 bg-transparent px-2 text-sm outline-none placeholder:text-muted-foreground sm:w-44 [&::-webkit-search-cancel-button]:hidden"
      />
      <span
        aria-live="polite"
        aria-atomic="true"
        className={`min-w-[3.25rem] shrink-0 text-right text-xs tabular-nums ${hasQuery && total === 0 ? "text-destructive" : "text-muted-foreground"}`}
        data-transcript-find-counter=""
      >
        {counter}
      </span>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className="h-7 w-7 rounded-full"
        onClick={() => onStep(-1)}
        disabled={total === 0}
        aria-label={t("Previous match")}
        title={t("Previous match")}
      >
        <ChevronUp className="h-4 w-4" aria-hidden="true" />
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className="h-7 w-7 rounded-full"
        onClick={() => onStep(1)}
        disabled={total === 0}
        aria-label={t("Next match")}
        title={t("Next match")}
      >
        <ChevronDown className="h-4 w-4" aria-hidden="true" />
      </Button>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        className="h-7 w-7 rounded-full"
        onClick={onClose}
        aria-label={t("Close search")}
        title={t("Close search")}
      >
        <X className="h-4 w-4" aria-hidden="true" />
      </Button>
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
      setElapsedSeconds((current) => (current === elapsed ? current : elapsed));
    };

    updateElapsed();
    const interval = window.setInterval(updateElapsed, 1000);
    return () => window.clearInterval(interval);
  }, [parsedStartMs, resetKey, status]);

  return status === "processing" ? formatElapsedDuration(elapsedSeconds) : duration || "";
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
      const res = await fetchWithTimeout(
        apiUrl(`/api/transcripts/${transcriptId}/summarize`),
        {
          method: "POST",
          credentials: "include",
        },
        15 * 60_000,
      );
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
      {isSummarizing ? <WavePhysicsLoader className="mr-1" size="micro" /> : <Sparkles className="mr-1 h-3 w-3" />}
      {isSummarizing ? t("Summarizing…") : t(label)}
    </Button>
  );
}

export default function TranscriptDetail() {
  const { id } = useParams();
  const [, setLocation] = useLocation();
  const { toast } = useToast();
  const { formatDate, formatLegacyDate, formatNumber, localeTag, t } = useI18n();
  const [copied, setCopied] = useState(false);
  const [copiedSummary, setCopiedSummary] = useState(false);
  const copyResetTimerRef = useRef<number | null>(null);
  const summaryCopyResetTimerRef = useRef<number | null>(null);
  const mainScrollRef = useAppScrollContainerRef();
  const summaryScrollRef = useRef<HTMLDivElement | null>(null);
  const isMobile = useIsMobile();
  const [isRetryingYoutube, setIsRetryingYoutube] = useState(false);
  const [isExporting, setIsExporting] = useState(false);
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
      const isRecentlyUpdated = Number.isFinite(updatedAtMs) ? Date.now() - updatedAtMs < 5 * 60 * 1000 : true;
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
  const readableFileTranscriptId =
    transcriptData?.type === "file" && transcriptData.status === "completed" ? transcriptData.id : "";
  useEffect(() => {
    // Podcast episodes are transcribed as file jobs; opening one marks it read.
    if (readableFileTranscriptId) void markPodcastTranscriptRead(queryClient, readableFileTranscriptId);
  }, [queryClient, readableFileTranscriptId]);
  // Podcast episodes are file transcripts; Back returns to their podcast.
  const podcastEpisodeQuery = useQuery<{
    episode: { id: string; status: string; subscriptionId?: string | null } | null;
  }>({
    queryKey: ["/api/podcasts/transcripts", id],
    enabled: Boolean(id) && transcriptData?.type === "file",
    staleTime: 60_000,
    queryFn: async ({ signal }) => {
      const res = await fetchWithTimeout(
        apiUrl(`/api/podcasts/transcripts/${encodeURIComponent(id || "")}`),
        { credentials: "include", signal },
        10_000,
      );
      if (!res.ok) throw new Error(await responseErrorMessage(res));
      return res.json();
    },
  });
  const podcastEpisode = transcriptData?.type === "file" ? podcastEpisodeQuery.data?.episode : null;
  const needsAutoSummarySetting = Boolean(
    transcriptData &&
    transcriptData.type !== "mic" &&
    transcriptData.status === "completed" &&
    !String(transcriptData.summary || "").trim() &&
    String(transcriptData.summaryStatus || "").toLowerCase() !== "failed",
  );
  // Auto-summary settings only affect completed file/YouTube jobs without a
  // summary. Mic details and active jobs no longer pay for an unrelated fetch.
  useQuery<SettingsResponse>({
    queryKey: ["/api/settings"],
    enabled: needsAutoSummarySetting,
    staleTime: 5 * 60_000,
    queryFn: async ({ signal }) => {
      const res = await fetchWithTimeout(
        apiUrl("/api/settings"),
        {
          credentials: "include",
          cache: "no-store",
          signal,
        },
        10_000,
      );
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
  const youtubeSourceUrl = transcript.type === "youtube" ? normalizeYoutubeSourceUrl(transcript.sourceUrl) : "";
  const summarySource = useMemo(() => String(transcript?.summary || "").trim(), [transcript?.summary]);
  const summaryFormat = transcript.summaryFormat === "html" ? "html" : "markdown";
  const preparedSummaryHtml = useMemo(
    () => (summaryFormat === "html" ? prepareSummaryHtml(summarySource) : { html: "", outline: [], plainText: "" }),
    [summaryFormat, summarySource],
  );
  const summaryMarkdown = useMemo(() => normalizeSummaryMarkdown(summarySource), [summarySource]);
  const summaryCopyText = summaryFormat === "html" ? preparedSummaryHtml.plainText : summaryMarkdown;
  const hasSummary = summarySource.length > 0;
  const isFailedTranscript = transcript.status === "failed";
  const failedContentLooksLikeErrorOnly = isFailureOnlyContent(transcript.status, transcript.content);
  const transcriptContent = failedContentLooksLikeErrorOnly ? "" : String(transcript.content || "");
  const hasExportableContent = !failedContentLooksLikeErrorOnly && Boolean(transcriptContent.trim() || hasSummary);
  const transcriptWordCount = useMemo(
    () => transcriptContent.trim().split(/\s+/).filter(Boolean).length,
    [transcriptContent],
  );
  const summaryStatus = String(transcript?.summaryStatus || (hasSummary ? "completed" : "idle")).toLowerCase();
  const summaryStepLower = String(transcript?.step || "").toLowerCase();
  const updatedAtMs = Date.parse(String(transcript?.updatedAt || ""));
  const isSummaryStep = summaryStepLower.includes("summariz");
  const isSummaryStepFresh = Number.isFinite(updatedAtMs) ? Date.now() - updatedAtMs < 3 * 60 * 1000 : true;
  const isSummaryInProgress =
    !isFailedTranscript &&
    summaryStatus !== "failed" &&
    !hasSummary &&
    (summaryStatus === "pending" || (isSummaryStep && isSummaryStepFresh));
  const isSummaryStepStale = !hasSummary && summaryStatus !== "failed" && isSummaryStep && !isSummaryStepFresh;
  const isSummaryFailed = summaryStatus === "failed";
  const summaryFailureMessage = friendlyRequestMessage(
    String(transcript?.summaryError || "").trim(),
    t("Summary generation failed."),
  );
  const summaryActionLabel = isSummaryFailed ? "Retry Summary" : "Summarize";
  const showHeaderSummaryAction =
    transcript.status === "completed" && !hasSummary && !isSummaryInProgress && !isSummaryFailed;
  const isFailedYoutubeTranscript = transcript?.status === "failed" && transcript?.type === "youtube";
  const rawFailureMessage = useMemo(
    () =>
      transcript.type === "mic" && !failedContentLooksLikeErrorOnly
        ? String(transcript.step || "")
        : extractFailureMessage(String(transcript.content || ""), String(transcript.step || "")),
    [failedContentLooksLikeErrorOnly, transcript.content, transcript.step, transcript.type],
  );
  const failedMessage = useMemo(
    () => (isFailedTranscript ? t(friendlyRequestMessage(rawFailureMessage, t("Transcription failed."))) : ""),
    [isFailedTranscript, rawFailureMessage, t],
  );
  const technicalFailureMessage = useMemo(() => {
    if (!isFailedYoutubeTranscript) return "";
    const technical = (rawFailureMessage || "").trim();
    if (!technical || technical === failedMessage) return "";
    return technical;
  }, [failedMessage, isFailedYoutubeTranscript, rawFailureMessage]);

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
      const res = await fetchWithTimeout(
        apiUrl("/api/youtube/transcribe"),
        {
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
        },
        15_000,
      );
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
  }, [
    id,
    isRetryingYoutube,
    queryClient,
    setLocation,
    t,
    toast,
    transcript?.sourceUrl,
    transcript?.title,
    transcript?.channel,
    transcript?.thumbnailUrl,
    transcript?.duration,
  ]);

  useEffect(
    () => () => {
      if (copyResetTimerRef.current !== null) {
        window.clearTimeout(copyResetTimerRef.current);
      }
      if (summaryCopyResetTimerRef.current !== null) {
        window.clearTimeout(summaryCopyResetTimerRef.current);
      }
    },
    [],
  );

  const handleCopyTranscript = useCallback(async () => {
    try {
      if (!navigator.clipboard?.writeText) {
        throw new Error(t("Clipboard API unavailable"));
      }
      await navigator.clipboard.writeText(transcriptContent);
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
  }, [t, toast, transcriptContent]);

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

  const handleExport = useCallback(
    async (format: "pdf" | "docx" | "summary-pdf") => {
      if (!id || isExporting || !hasExportableContent) return;
      if (format === "summary-pdf" && !hasSummary) return;
      setIsExporting(true);
      try {
        const title = transcript?.title || t("Transcript");
        const result =
          format === "docx"
            ? await saveTranscriptExport(`/api/transcripts/${id}/export/docx`, `${title}.docx`)
            : await saveTranscriptPdfExport({
                transcriptId: id,
                variant: format === "summary-pdf" ? "summary" : "full",
                fallbackName: `${title}.pdf`,
                labels: {
                  locale: localeTag,
                  transcript: t("Transcript"),
                  summary: t("Summary"),
                  date: t("Date"),
                  duration: t("Duration"),
                  pageOf: t("Page {{page}} of {{total}}", { page: "{page}", total: "{total}" }),
                  speaker: (speakerNumber) => t("Speaker {{number}}", { number: formatNumber(speakerNumber) }),
                },
              });
        if (result.status === "saved") {
          toast({
            title: result.desktop ? t("Export saved") : t("Download started"),
            description: t("{{filename}} is ready.", { filename: result.filename }),
            duration: 3000,
          });
        }
      } catch (error) {
        toast({
          title: t("Export failed"),
          description: friendlyError(error, t("Scriber could not save the transcript export. Please try again.")),
          variant: "destructive",
          duration: 5000,
        });
      } finally {
        setIsExporting(false);
      }
    },
    [formatNumber, hasExportableContent, hasSummary, id, isExporting, localeTag, t, toast, transcript?.title],
  );

  const getBackLink = () => {
    switch (transcript?.type) {
      case "youtube":
        return "/youtube";
      case "file":
        if (podcastEpisode) {
          return podcastEpisode.subscriptionId
            ? `/podcasts?subscription=${encodeURIComponent(podcastEpisode.subscriptionId)}`
            : "/podcasts";
        }
        return "/file";
      default:
        return "/";
    }
  };

  // Controlled accordion state. A transcript that already has a summary opens
  // on it; a summary arriving later is added without collapsing what the user
  // is reading.
  const [accordionValue, setAccordionValue] = useState<string[]>(
    String(transcriptQuery.data?.summary || "").trim() ? ["summary"] : ["transcript"],
  );
  const hasLoadedTranscript = transcriptQuery.data !== undefined;
  const seenSummaryRef = useRef<boolean | null>(null);
  useEffect(() => {
    if (!hasLoadedTranscript) return;
    const previous = seenSummaryRef.current;
    seenSummaryRef.current = hasSummary;
    if (previous === null) {
      if (hasSummary)
        setAccordionValue((current) => (current.length === 1 && current[0] === "summary" ? current : ["summary"]));
      return;
    }
    if (hasSummary && !previous) {
      setAccordionValue((current) => (current.includes("summary") ? current : ["summary", ...current]));
    }
  }, [hasLoadedTranscript, hasSummary]);

  // Find in transcript.
  const transcriptBlocks = useMemo(() => parseTranscriptContent(transcriptContent), [transcriptContent]);
  const transcriptSearchIndex = useMemo(() => prepareTranscriptSearchIndex(transcriptBlocks), [transcriptBlocks]);
  const canFindInTranscript =
    transcriptBlocks.length > 0 && !transcriptQuery.isLoading && transcript.status !== "processing";
  const [findOpen, setFindOpen] = useState(false);
  const [findQuery, setFindQuery] = useState("");
  const [activeMatch, setActiveMatch] = useState(0);
  const [findFocusRequest, setFindFocusRequest] = useState(0);
  const findInputRef = useRef<HTMLInputElement | null>(null);
  const findToggleRef = useRef<HTMLButtonElement | null>(null);
  const restoreFindToggleFocusRef = useRef(false);
  const transcriptContentRef = useRef<HTMLDivElement | null>(null);
  const deferredFindQuery = useDeferredValue(findOpen ? findQuery : "");
  const findMatches = useMemo(
    () => findPreparedTranscriptMatches(transcriptSearchIndex, deferredFindQuery),
    [deferredFindQuery, transcriptSearchIndex],
  );
  const matchesByBlock = useMemo(() => {
    const grouped = new Map<number, BlockMatches>();
    findMatches.forEach((match, index) => {
      const entry = grouped.get(match.blockIndex);
      if (entry) entry.matches.push(match);
      else grouped.set(match.blockIndex, { offset: index, matches: [match] });
    });
    return grouped;
  }, [findMatches]);
  const activeMatchIndex = findMatches.length ? Math.min(activeMatch, findMatches.length - 1) : -1;

  useEffect(() => {
    setActiveMatch(0);
  }, [deferredFindQuery]);

  const openFind = useCallback(() => {
    setAccordionValue((current) => (current.includes("transcript") ? current : [...current, "transcript"]));
    setFindOpen(true);
    setFindFocusRequest((request) => request + 1);
  }, []);
  const closeFind = useCallback(() => {
    restoreFindToggleFocusRef.current = true;
    setFindOpen(false);
    setFindQuery("");
    setActiveMatch(0);
  }, []);
  const stepFind = useCallback(
    (direction: 1 | -1) => {
      const total = findMatches.length;
      setActiveMatch((current) => Math.max(0, cycleMatchIndex(Math.min(current, total - 1), total, direction)));
    },
    [findMatches.length],
  );

  useEffect(() => {
    if (!findFocusRequest) return;
    const input = findInputRef.current;
    if (!input) return;
    input.focus();
    input.select();
  }, [findFocusRequest, findOpen]);

  useEffect(() => {
    if (findOpen || !restoreFindToggleFocusRef.current) return;
    restoreFindToggleFocusRef.current = false;
    findToggleRef.current?.focus();
  }, [findOpen]);

  useEffect(() => {
    if (!canFindInTranscript) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.altKey || event.shiftKey || !(event.ctrlKey || event.metaKey)) return;
      if (event.key.toLowerCase() !== "f" && event.code !== "KeyF") return;
      if (document.querySelector('[role="dialog"][data-state="open"]')) return;
      event.preventDefault();
      openFind();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [canFindInTranscript, openFind]);

  useEffect(() => {
    if (activeMatchIndex < 0) return;
    const mark = transcriptContentRef.current?.querySelector<HTMLElement>('mark[data-active="true"]');
    if (!mark || typeof mark.scrollIntoView !== "function") return;
    mark.scrollIntoView({ block: "center", inline: "nearest", behavior: prefersReducedMotion() ? "auto" : "smooth" });
  }, [activeMatchIndex, findMatches]);
  const showSummaryToc =
    summaryFormat === "html" && preparedSummaryHtml.outline.length >= 2 && accordionValue.includes("summary");
  const summaryTocTitle = useMemo(
    () => summaryTableOfContentsTitle(preparedSummaryHtml.plainText, transcript.language),
    [preparedSummaryHtml.plainText, transcript.language],
  );

  return (
    <div className={`transcript-detail-page min-h-full bg-background${hasSummary ? " has-summary-reader" : ""}`}>
      {/* Header Toolbar */}
      <div className="transcript-detail-header-sticky sticky top-0 z-40 px-4 pt-4 md:px-12 lg:px-8 xl:px-12">
        <header
          data-transcript-detail-header="true"
          className={`transcript-detail-header-shell flex min-h-16 items-center justify-between gap-4 backdrop-blur-xl${showSummaryToc ? " has-summary-toc" : ""}`}
        >
          <div className="flex min-w-0 flex-1 items-center gap-4">
            <Link href={getBackLink()}>
              <Button variant="ghost" size="icon" className="-ml-2 shrink-0" aria-label={t("Go back")}>
                <ArrowLeft className="w-5 h-5 text-muted-foreground" />
              </Button>
            </Link>
            <div className="min-w-0 flex-1">
              <h1
                className="truncate font-heading text-[18px] font-semibold tracking-[-0.025em] text-foreground md:text-[24px]"
                title={transcript?.title || t("Transcript")}
              >
                {transcript?.title || t("Transcript")}
              </h1>
              <p className="truncate text-xs text-muted-foreground">
                {dateDisplay} • <span>{durationDisplay}</span>
              </p>
            </div>
          </div>

          <div className="flex shrink-0 items-center gap-2">
            {youtubeSourceUrl && (
              <Button asChild variant="outline" size="sm" className="hidden md:inline-flex">
                <a
                  href={youtubeSourceUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  aria-label={t("Open on YouTube")}
                  onClick={(event) => {
                    if (!isTauriRuntime()) return;
                    event.preventDefault();
                    void openYoutubeSourceUrl(youtubeSourceUrl);
                  }}
                  data-testid="youtube-source-link"
                >
                  <Youtube className="text-red-600 dark:text-red-400" aria-hidden="true" />
                  <span className="hidden 2xl:inline">{t("Open on YouTube")}</span>
                  <ExternalLink className="hidden 2xl:block" aria-hidden="true" />
                </a>
              </Button>
            )}
            {transcriptContent && (
              <Button
                variant="default"
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
                <Button
                  variant="outline"
                  size="sm"
                  className="hidden md:flex data-[state=open]:bg-accent"
                  style={{ transform: "none" }}
                  disabled={!hasExportableContent}
                  type="button"
                >
                  <Download className="w-4 h-4 mr-2" /> {t("Export")}
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem
                  disabled={isExporting || !hasExportableContent}
                  onSelect={() => void handleExport("pdf")}
                >
                  {isExporting ? (
                    <WavePhysicsLoader className="mr-2" size="inline" />
                  ) : (
                    <FileText className="w-4 h-4 mr-2" />
                  )}{" "}
                  {t("Export as PDF")}
                </DropdownMenuItem>
                {hasSummary && (
                  <DropdownMenuItem disabled={isExporting} onSelect={() => void handleExport("summary-pdf")}>
                    {isExporting ? (
                      <WavePhysicsLoader className="mr-2" size="inline" />
                    ) : (
                      <ScrollText className="w-4 h-4 mr-2" />
                    )}{" "}
                    {t("Export summary as PDF")}
                  </DropdownMenuItem>
                )}
                <DropdownMenuItem
                  disabled={isExporting || !hasExportableContent}
                  onSelect={() => void handleExport("docx")}
                >
                  {isExporting ? (
                    <WavePhysicsLoader className="mr-2" size="inline" />
                  ) : (
                    <FileText className="w-4 h-4 mr-2" />
                  )}{" "}
                  {t("Export as DOCX")}
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>

            <DropdownMenu modal={false}>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="outline"
                  size="icon"
                  className="md:hidden"
                  aria-label={t("Open transcript actions")}
                  type="button"
                >
                  <Download className="w-4 h-4" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                {youtubeSourceUrl && (
                  <DropdownMenuItem asChild>
                    <a
                      href={youtubeSourceUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      onClick={(event) => {
                        if (!isTauriRuntime()) return;
                        event.preventDefault();
                        void openYoutubeSourceUrl(youtubeSourceUrl);
                      }}
                    >
                      <Youtube className="mr-2 h-4 w-4 text-red-600 dark:text-red-400" aria-hidden="true" />
                      {t("Open on YouTube")}
                      <ExternalLink className="ml-auto h-3.5 w-3.5 text-muted-foreground" aria-hidden="true" />
                    </a>
                  </DropdownMenuItem>
                )}
                {transcriptContent && (
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
                  disabled={isExporting || !hasExportableContent}
                  onSelect={() => void handleExport("pdf")}
                >
                  {isExporting ? (
                    <WavePhysicsLoader className="mr-2" size="inline" />
                  ) : (
                    <FileText className="w-4 h-4 mr-2" />
                  )}{" "}
                  {t("Export as PDF")}
                </DropdownMenuItem>
                {hasSummary && (
                  <DropdownMenuItem disabled={isExporting} onSelect={() => void handleExport("summary-pdf")}>
                    {isExporting ? (
                      <WavePhysicsLoader className="mr-2" size="inline" />
                    ) : (
                      <ScrollText className="w-4 h-4 mr-2" />
                    )}{" "}
                    {t("Export summary as PDF")}
                  </DropdownMenuItem>
                )}
                <DropdownMenuItem
                  disabled={isExporting || !hasExportableContent}
                  onSelect={() => void handleExport("docx")}
                >
                  {isExporting ? (
                    <WavePhysicsLoader className="mr-2" size="inline" />
                  ) : (
                    <FileText className="w-4 h-4 mr-2" />
                  )}{" "}
                  {t("Export as DOCX")}
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
            {transcript.status === "processing" && (
              <TranscriptStopButton
                transcriptId={id!}
                onStop={() => queryClient.invalidateQueries({ queryKey: ["/api/transcripts", id] })}
              />
            )}
            {transcript.type === "file" &&
              transcript.resumeAvailable === true &&
              (transcript.status === "failed" || transcript.status === "stopped") && (
                <FileTranscriptResumeButton key={transcript.id} transcriptId={transcript.id} />
              )}
            {showHeaderSummaryAction && (
              <div className="hidden md:block">
                <SummarizeButton
                  transcriptId={id}
                  label={summaryActionLabel}
                  onComplete={() => queryClient.invalidateQueries({ queryKey: ["/api/transcripts", id] })}
                />
              </div>
            )}
            {showHeaderSummaryAction && (
              <div className="md:hidden">
                <SummarizeButton
                  transcriptId={id}
                  label={summaryActionLabel}
                  onComplete={() => queryClient.invalidateQueries({ queryKey: ["/api/transcripts", id] })}
                />
              </div>
            )}
          </div>
        </header>
      </div>

      {/* Main Content */}
      <div className="transcript-detail-scroll p-4 md:p-8 md:px-12 lg:px-8 xl:px-12">
        <div
          className={
            showSummaryToc ? "transcript-detail-shell has-summary-toc space-y-6" : "transcript-detail-shell space-y-6"
          }
        >
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
              <WavePhysicsLoader size="compact" label={t("Loading…")} />
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

          {isFailedTranscript && (
            <div className="space-y-2">
              <QueryErrorState
                title={t(isFailedYoutubeTranscript ? "YouTube transcription failed" : "Transcription failed")}
                className="text-foreground [&>svg]:text-red-500 dark:[&>svg]:text-red-400"
                description={failedMessage || t("The transcription failed. Please try again.")}
                onRetry={
                  isFailedYoutubeTranscript
                    ? () => {
                        void retryYoutubeTranscription();
                      }
                    : undefined
                }
              />
              {isFailedYoutubeTranscript && requiresYouTubeSignIn(rawFailureMessage) && (
                <YouTubeSessionSettings
                  key={transcript.id}
                  recoveryKey={transcript.id}
                  videoUrl={transcript.sourceUrl}
                  onConnected={retryYoutubeTranscription}
                />
              )}
              {transcript.type === "file" && transcript.resumeAvailable !== true && (
                <FileTranscriptRetryButton key={transcript.id} transcriptId={id!} resumeAvailable={false} />
              )}
              {technicalFailureMessage && (
                <p className="text-xs text-muted-foreground px-1">
                  {t("Technical details")}: {technicalFailureMessage}
                </p>
              )}
            </div>
          )}

          {isSummaryFailed && (
            <div className="space-y-3">
              <QueryErrorState title={t("Summary generation failed")} description={t(summaryFailureMessage)} />
              <SummarizeButton
                transcriptId={id}
                label={t("Retry Summary")}
                onComplete={() => queryClient.invalidateQueries({ queryKey: ["/api/transcripts", id] })}
              />
            </div>
          )}

          <TranscriptionDiarizationNotice code={transcript.diarizationFallback} />
          <TranscriptionBoundaryNotice warnings={transcript.chunkBoundaryWarnings} />
          <div key={id} className="transcript-summary-layout">
            {showSummaryToc && (
              <SummaryTableOfContents
                outline={preparedSummaryHtml.outline}
                scrollContainerRef={isMobile ? mainScrollRef : summaryScrollRef}
                scrollPage={isMobile}
                title={summaryTocTitle}
              />
            )}
            {/* Accordion with Summary and Transcript */}
            <Accordion
              type="multiple"
              value={accordionValue}
              onValueChange={setAccordionValue}
              className="transcript-detail-accordion space-y-4"
            >
              {/* Summary Section */}
              <AccordionItem value="summary" className="transcript-summary-panel neu-recording-row">
                <AccordionTrigger className="transcript-summary-panel__trigger px-4 py-3 hover:no-underline">
                  <div className="flex items-center gap-2">
                    <Sparkles className="summary-accent-icon w-4 h-4" />
                    <span className="text-base font-semibold tracking-tight">{t("Summary")}</span>
                    {isSummaryInProgress && (
                      <span className="flex items-center gap-1 text-xs text-muted-foreground ml-2">
                        <WavePhysicsLoader size="micro" />
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
                <AccordionContent ref={summaryScrollRef} data-summary-scroll="true" tabIndex={0} className="px-4 pb-4">
                  {hasSummary ? (
                    summaryFormat === "html" ? (
                      <TranscriptSummaryDocument prepared={preparedSummaryHtml} />
                    ) : (
                      <div className="summary-document summary-document--markdown">
                        <MemoizedMarkdown>{summaryMarkdown}</MemoizedMarkdown>
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
                <div className="flex flex-wrap items-center gap-x-1 gap-y-2" data-transcript-section-header="">
                  <div className="min-w-[10rem] flex-1">
                    <AccordionTrigger className="px-4 py-3 hover:no-underline">
                      <div className="flex min-w-0 items-center gap-2 pr-3">
                        <FileText className="h-4 w-4 shrink-0 text-blue-600" />
                        <span className="text-base font-semibold tracking-tight">{t("Transcript")}</span>
                        {transcriptWordCount > 0 && (
                          <span className="truncate text-xs font-normal text-muted-foreground">
                            {t(transcriptWordCount === 1 ? "{{count}} word" : "{{count}} words", {
                              count: formatNumber(transcriptWordCount),
                            })}
                          </span>
                        )}
                      </div>
                    </AccordionTrigger>
                  </div>
                  {canFindInTranscript &&
                    (findOpen ? (
                      <div className="ml-auto px-3 pb-2 sm:pb-0">
                        <TranscriptFindBar
                          inputRef={findInputRef}
                          query={findQuery}
                          total={findMatches.length}
                          activeMatch={activeMatchIndex}
                          onQueryChange={setFindQuery}
                          onStep={stepFind}
                          onClose={closeFind}
                        />
                      </div>
                    ) : (
                      <Button
                        ref={findToggleRef}
                        type="button"
                        variant="ghost"
                        size="icon"
                        className="mr-2 h-8 w-8 shrink-0 rounded-full text-muted-foreground"
                        onClick={openFind}
                        aria-label={t("Find in transcript")}
                        title={t("Find in transcript ({{shortcut}})", { shortcut: findShortcutLabel(t) })}
                        data-transcript-find-toggle=""
                      >
                        <Search className="h-4 w-4" aria-hidden="true" />
                      </Button>
                    ))}
                </div>
                <AccordionContent tabIndex={0} className="px-4 pb-4">
                  <div ref={transcriptContentRef} className="transcript-content">
                    {transcriptQuery.isLoading ? (
                      t("Loading…")
                    ) : transcript.status === "processing" ? (
                      <span className="text-muted-foreground italic"></span>
                    ) : transcriptBlocks.length > 0 ? (
                      <TranscriptBlocksView
                        blocks={transcriptBlocks}
                        matchesByBlock={matchesByBlock}
                        activeMatch={activeMatchIndex}
                      />
                    ) : (
                      t("No transcript text captured.")
                    )}
                  </div>
                </AccordionContent>
              </AccordionItem>
            </Accordion>
          </div>
        </div>
      </div>
    </div>
  );
}

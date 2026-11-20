import { useCallback, useState, useEffect, memo, useMemo, useRef, useSyncExternalStore } from "react";
import { useDropzone } from "react-dropzone";
import { AlertCircle, UploadCloud, FileAudio, FileVideoCamera, XCircle, Square, ArrowRight } from "lucide-react";
import { InfoTooltip } from "@/components/ui/info-tooltip";
import { Card } from "@/components/ui/card";
import { FileImportQueue } from "@/components/file-import-queue";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { useLocation } from "wouter";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiUrl } from "@/lib/backend";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";
import { useToast } from "@/hooks/use-toast";
import { EmptyState } from "@/components/ui/empty-state";
import { useTranscriptAutoRefresh } from "@/hooks/use-transcript-auto-refresh";
import { DeleteActionButton } from "@/components/ui/delete-action-button";
import { CopyActionButton } from "@/components/ui/copy-action-button";
import { PageIntro } from "@/components/page-intro";
import { TranscriptionHistoryToolbar } from "@/components/transcription-history-toolbar";
import { TranscriptHistoryPanel } from "@/components/transcript-history-panel";
import { TranscriptSummaryRetryButton } from "@/components/transcript-summary-retry-button";
import { FileTranscriptRetryButton } from "@/components/file-transcript-retry-button";
import { TranscriptStopButton } from "@/components/transcript-stop-button";
import { VirtualTranscriptHistory } from "@/components/virtual-transcript-history";
import { transcriptHistoryItemKey } from "@/lib/virtual-transcript-history-layout";
import { ErrorShake } from "@/components/ui/error-shake";
import { transcriptHistoryQueryKey, useTranscriptHistoryQuery } from "@/hooks/use-transcript-history-query";
import {
  getFileUploadSnapshot,
  startFileUploadBatch,
  subscribeFileUpload,
  type FileUploadLocalizedText,
} from "@/lib/file-upload-store";
import type {
  ApiMessageResponse,
  SettingsResponse,
  TranscriptDeleteResponse,
  TranscriptDetailResponse,
  TranscriptHistoryItem,
} from "@/lib/api-types";
import { formatNumberNow, translateNow, useI18n } from "@/i18n";
import { useTranscriptHistoryPanelState } from "@/hooks/use-transcript-history-panel-state";

const DEFAULT_COMPRESSION_THRESHOLD_BYTES = 50 * 1024 * 1024;
const VIDEO_EXTENSIONS = new Set([".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v", ".flv", ".wmv"]);

interface FileDropError {
  reason: string;
  fileName?: string;
}

function getFileExtension(fileName: string): string {
  const dotIndex = fileName.lastIndexOf(".");
  return dotIndex >= 0 ? fileName.slice(dotIndex).toLowerCase() : "";
}

function inferServerProcessingText(file: File, compressionThresholdBytes: number): FileUploadLocalizedText {
  const ext = getFileExtension(file.name);
  if (VIDEO_EXTENSIONS.has(ext)) {
    return { key: "Extracting audio…" };
  }
  if (file.size > compressionThresholdBytes) {
    return { key: "Optimizing audio…" };
  }
  return { key: "Preparing audio…" };
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

const AUDIO_EXTENSIONS = new Set([".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac", ".opus", ".wma", ".aif", ".aiff"]);

type FileMediaKind = "audio" | "video" | "unknown";

// File records keep the original file name as their title; the source path is
// never sent to the client, so the extension is the only reliable media hint.
function fileMediaKind(item: Pick<TranscriptHistoryItem, "title">): FileMediaKind {
  const ext = getFileExtension(String(item.title || "").trim());
  if (VIDEO_EXTENSIONS.has(ext)) return "video";
  if (AUDIO_EXTENSIONS.has(ext)) return "audio";
  return "unknown";
}

function FileMediaIcon({ item, className }: { item: Pick<TranscriptHistoryItem, "title">; className: string }) {
  const { t } = useI18n();
  const kind = fileMediaKind(item);
  if (kind === "unknown") {
    return <FileAudio className={className} aria-hidden="true" />;
  }
  const Icon = kind === "video" ? FileVideoCamera : FileAudio;
  return (
    <Icon
      className={className}
      role="img"
      aria-label={kind === "video" ? t("Video file") : t("Audio file")}
      data-media-kind={kind}
    />
  );
}

function formatByteLimit(bytes: number, formatNumber: ReturnType<typeof useI18n>["formatNumber"]): string {
  const gib = 1024 * 1024 * 1024;
  if (bytes >= gib) return `${formatNumber(bytes / gib, { maximumFractionDigits: 1 })} GB`;
  return `${formatNumber(bytes / (1024 * 1024), { maximumFractionDigits: 0 })} MB`;
}

// File records reuse `channel` for a compact size label such as "12.3MB".
function formatFileSizeLabel(value: string, formatNumber: ReturnType<typeof useI18n>["formatNumber"]): string {
  const match = /^(\d+(?:\.\d+)?)\s*(B|KB|MB|GB)$/i.exec(value.trim());
  if (!match) return value;
  return `${formatNumber(Number(match[1]), { maximumFractionDigits: 1 })} ${match[2].toUpperCase()}`;
}

type FileHistoryStatus = "processing" | "failed" | "summary_failed" | "stopped" | "ready";

// Tile tones live in capture-polish.css: ready tiles carry the section accent,
// status tiles keep their semantic colors.
const FILE_STATUS_TILE_CLASSES: Record<FileHistoryStatus, string> = {
  failed: "file-history-icon--danger",
  summary_failed: "file-history-icon--danger",
  processing: "file-history-icon--progress",
  stopped: "file-history-icon--stopped",
  ready: "file-history-icon--media",
};

function FileHistoryIcon({
  item,
  status,
  size,
}: {
  item: TranscriptHistoryItem;
  status: FileHistoryStatus;
  size: "list" | "grid";
}) {
  const iconClassName = size === "grid" ? "h-6 w-6" : "h-5 w-5";
  return (
    <div
      className={`file-history-icon flex shrink-0 items-center justify-center ${
        size === "grid" ? "h-12 w-12 rounded-[13px]" : "h-10 w-10 rounded-[12px]"
      } ${FILE_STATUS_TILE_CLASSES[status]}`}
    >
      {status === "failed" ? (
        <XCircle className={iconClassName} aria-hidden="true" />
      ) : status === "summary_failed" ? (
        <AlertCircle className={iconClassName} aria-hidden="true" />
      ) : status === "stopped" ? (
        <Square className={iconClassName} aria-hidden="true" />
      ) : (
        <FileMediaIcon item={item} className={`${iconClassName} stroke-[1.65px]`} />
      )}
    </div>
  );
}

function fileHistoryStatus(item: TranscriptHistoryItem): FileHistoryStatus {
  if (item.status === "failed") return "failed";
  if (item.summaryStatus === "pending" || item.status === "processing") return "processing";
  if (item.summaryStatus === "failed") return "summary_failed";
  if (item.status === "stopped") return "stopped";
  return "ready";
}

// Memoized FileCard to prevent unnecessary re-renders
interface FileCardProps {
  item: TranscriptHistoryItem;
  viewMode: "list" | "grid";
  isDeleting: boolean;
  isCopying: boolean;
  onDelete: (e: React.MouseEvent, id: string) => void;
  onCopy: (e: React.MouseEvent, id: string) => void;
  onSummaryRetryComplete: (id: string) => void;
  onNavigate: (id: string) => void;
  onHover?: (id: string) => void;
}

const FileCard = memo(function FileCard({
  item,
  viewMode,
  isDeleting,
  isCopying,
  onDelete,
  onCopy,
  onSummaryRetryComplete,
  onNavigate,
  onHover,
}: FileCardProps) {
  const { formatFullDateTime, formatLegacyDate, formatNumber, formatRelativeDate, t } = useI18n();
  const deletingClasses = isDeleting ? "pointer-events-none opacity-[0.55] scale-[0.985]" : "opacity-100 scale-100";
  const historyStatus = fileHistoryStatus(item);
  const dateLabel = (item.createdAt && formatRelativeDate(item.createdAt)) || formatLegacyDate(item.date);
  const dateTitle = (item.createdAt && formatFullDateTime(item.createdAt)) || dateLabel;
  const dateElement = (
    <time dateTime={item.createdAt} title={dateTitle} className="tabular-nums">
      {dateLabel}
    </time>
  );

  return (
    <div className="w-full">
      <Card
        className={`file-history-card perf-scroll-item ${viewMode === "grid" ? "perf-scroll-grid min-h-[176px]" : ""} cursor-pointer rounded-[20px] p-4 group transform-gpu ${deletingClasses}`}
        onClick={() => onNavigate(item.id)}
        onMouseEnter={() => onHover?.(item.id)}
      >
        {viewMode === "list" ? (
          // List view
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex min-w-0 flex-1 items-center gap-4">
              <FileHistoryIcon item={item} status={historyStatus} size="list" />
              <div className="min-w-0 flex-1">
                <h3>
                  <button
                    type="button"
                    className="line-clamp-2 min-h-11 w-full rounded-sm text-left font-heading text-[14px] font-medium leading-[1.4] text-foreground outline-none transition-colors duration-200 group-hover:text-primary focus-visible:ring-2 focus-visible:ring-ring/60 sm:min-h-0"
                    onClick={(event) => {
                      event.stopPropagation();
                      onNavigate(item.id);
                    }}
                  >
                    {item.title}
                  </button>
                </h3>
                <div className="flex items-center gap-3 text-xs text-muted-foreground mt-1">
                  {item.channel && <span>{formatFileSizeLabel(item.channel, formatNumber)}</span>}
                  {item.channel && <span>•</span>}
                  <span>{item.duration}</span>
                  <span>•</span>
                  {dateElement}
                </div>
                {(historyStatus === "failed" || historyStatus === "summary_failed") && (
                  <p className="mt-1 select-text break-all font-mono text-xs text-muted-foreground">
                    {t("Reference: {{id}}", { id: item.id })}
                  </p>
                )}
              </div>
            </div>
            <div className="flex flex-wrap items-center justify-end gap-2">
              {historyStatus === "processing" ? (
                <Badge
                  variant="outline"
                  className="text-blue-600 border-blue-200 bg-blue-50 text-ui-micro flex items-center gap-1"
                >
                  <span
                    className="h-1.5 w-1.5 shrink-0 rounded-full bg-current motion-safe:animate-pulse"
                    aria-hidden="true"
                  />
                  {item.summaryStatus === "pending"
                    ? t("Summarizing…")
                    : localizedProcessingStep(item.step, "Processing", t, formatNumber)}
                </Badge>
              ) : historyStatus === "failed" ? (
                <Badge variant="outline" className="text-red-600 border-red-200 bg-red-50 text-ui-micro">
                  {t("Failed")}
                </Badge>
              ) : historyStatus === "summary_failed" ? (
                <TranscriptSummaryRetryButton
                  transcriptId={item.id}
                  transcriptTitle={item.title}
                  onComplete={onSummaryRetryComplete}
                />
              ) : historyStatus === "stopped" ? (
                <Badge variant="outline" className="text-yellow-600 border-yellow-200 bg-yellow-50 text-ui-micro">
                  {t("Stopped")}
                </Badge>
              ) : null}
              {historyStatus === "failed" && <FileTranscriptRetryButton transcriptId={item.id} />}
              <CopyActionButton
                onClick={(e) => onCopy(e, item.id)}
                disabled={isCopying}
                copied={isCopying}
                title={t("Copy transcript")}
                ariaLabel={t("Copy transcript {{title}}", { title: item.title })}
                className="opacity-100 md:opacity-0 md:group-hover:opacity-100 md:group-focus-within:opacity-100 transition-opacity"
              />
              <DeleteActionButton
                onClick={(e) => onDelete(e, item.id)}
                disabled={isDeleting}
                loading={isDeleting}
                title={t("Delete transcript")}
                ariaLabel={t("Delete transcript {{title}}", { title: item.title })}
                className="opacity-100 md:opacity-0 md:group-hover:opacity-100 md:group-focus-within:opacity-100 transition-opacity"
              />
            </div>
          </div>
        ) : (
          // Grid view
          <div className="flex flex-col h-full">
            <div className="flex items-start justify-between gap-3 mb-3">
              <FileHistoryIcon item={item} status={historyStatus} size="grid" />
              <div className="flex min-w-0 flex-1 items-center justify-end gap-1">
                {historyStatus === "processing" ? (
                  <Badge
                    variant="outline"
                    className="min-w-0 max-w-full text-blue-600 border-blue-200 bg-blue-50 text-ui-micro flex items-center gap-1"
                    title={item.summaryStatus === "pending" ? t("Summarizing…") : undefined}
                  >
                    <span
                      className="h-1.5 w-1.5 shrink-0 rounded-full bg-current motion-safe:animate-pulse"
                      aria-hidden="true"
                    />
                    {item.summaryStatus === "pending" ? (
                      <span className="min-w-0 truncate">{t("Summarizing")}</span>
                    ) : null}
                  </Badge>
                ) : historyStatus === "failed" ? (
                  <Badge variant="outline" className="text-red-600 border-red-200 bg-red-50 text-ui-micro">
                    {t("Failed")}
                  </Badge>
                ) : historyStatus === "summary_failed" ? (
                  <TranscriptSummaryRetryButton
                    transcriptId={item.id}
                    transcriptTitle={item.title}
                    onComplete={onSummaryRetryComplete}
                    compact
                  />
                ) : historyStatus === "stopped" ? (
                  <Badge variant="outline" className="text-yellow-600 border-yellow-200 bg-yellow-50 text-ui-micro">
                    {t("Stopped")}
                  </Badge>
                ) : null}
              </div>
            </div>
            <h3 className="mb-2">
              <button
                type="button"
                className="line-clamp-2 min-h-11 w-full rounded-sm text-left font-heading text-[14px] font-medium leading-[1.35] text-foreground outline-none transition-colors duration-200 group-hover:text-primary focus-visible:ring-2 focus-visible:ring-ring/60 sm:min-h-0"
                onClick={(event) => {
                  event.stopPropagation();
                  onNavigate(item.id);
                }}
              >
                {item.title}
              </button>
            </h3>
            {(historyStatus === "failed" || historyStatus === "summary_failed") && (
              <p className="mb-2 select-text break-all font-mono text-xs text-muted-foreground">
                {t("Reference: {{id}}", { id: item.id })}
              </p>
            )}
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground mt-auto">
              <span>{item.duration}</span>
              <span>•</span>
              {dateElement}
            </div>
            <div className="flex flex-wrap items-center justify-end mt-2 gap-1">
              {historyStatus === "failed" && <FileTranscriptRetryButton transcriptId={item.id} compact />}
              <CopyActionButton
                onClick={(e) => onCopy(e, item.id)}
                disabled={isCopying}
                copied={isCopying}
                title={t("Copy transcript")}
                ariaLabel={t("Copy transcript {{title}}", { title: item.title })}
                size="sm"
                className="opacity-100 md:opacity-0 md:group-hover:opacity-100 md:group-focus-within:opacity-100 transition-opacity"
              />
              <DeleteActionButton
                onClick={(e) => onDelete(e, item.id)}
                disabled={isDeleting}
                loading={isDeleting}
                title={t("Delete transcript")}
                ariaLabel={t("Delete transcript {{title}}", { title: item.title })}
                size="sm"
                className="opacity-100 md:opacity-0 md:group-hover:opacity-100 md:group-focus-within:opacity-100 transition-opacity"
              />
            </div>
          </div>
        )}
      </Card>
    </div>
  );
});

export default function FileTranscribe() {
  const [, setLocation] = useLocation();
  const { toast } = useToast();
  const { formatNumber, t } = useI18n();
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [copyingId, setCopyingId] = useState<string | null>(null);
  const [dropError, setDropError] = useState<FileDropError | null>(null);
  const deletingRef = useRef<string | null>(null);
  const copyingRef = useRef<string | null>(null);
  const copyResetTimerRef = useRef<number | null>(null);
  const uploadSnapshot = useSyncExternalStore(subscribeFileUpload, getFileUploadSnapshot, getFileUploadSnapshot);
  const isUploading = uploadSnapshot.status === "uploading" || uploadSnapshot.status === "server_processing";
  const queryClient = useQueryClient();
  const {
    debouncedSearch,
    searchValue: searchQuery,
    setSearchValue: setSearchQuery,
    setViewMode,
    viewMode,
  } = useTranscriptHistoryPanelState({ defaultViewMode: "list" });
  const transcriptsQueryKey = useMemo(() => transcriptHistoryQueryKey("file", debouncedSearch), [debouncedSearch]);

  useEffect(
    () => () => {
      if (copyResetTimerRef.current !== null) {
        window.clearTimeout(copyResetTimerRef.current);
      }
    },
    [],
  );

  const transcriptsQuery = useTranscriptHistoryQuery<TranscriptHistoryItem>({ type: "file", q: debouncedSearch });
  const recentFromBackend = transcriptsQuery.items;
  const settingsQuery = useQuery<SettingsResponse>({
    queryKey: ["/api/settings"],
    queryFn: async ({ signal }) => {
      const res = await fetchWithTimeout(apiUrl("/api/settings"), { credentials: "include", signal }, 10_000);
      if (!res.ok) throw new Error(t("Failed to load settings"));
      return (await res.json()) as SettingsResponse;
    },
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });
  const fileUploadLimits = settingsQuery.data?.fileUploadLimits;
  const compressionThresholdBytes =
    Number(fileUploadLimits?.compressionThresholdBytes) || DEFAULT_COMPRESSION_THRESHOLD_BYTES;
  // A short, provider-neutral summary lives in the upload card; the provider
  // specifics stay one tap away in its info tooltip.
  const uploadLimits = useMemo(() => {
    const thresholdLabel = formatByteLimit(compressionThresholdBytes, formatNumber);
    const audioMaxBytes = Number(fileUploadLimits?.audioMaxBytes) || 0;
    const audioLimitLabel = audioMaxBytes > 0 ? formatByteLimit(audioMaxBytes, formatNumber) : "";
    const summary = audioLimitLabel
      ? t("Up to {{limit}} · large files are optimized automatically", { limit: audioLimitLabel })
      : t("Large files are optimized automatically");

    const details: string[] = [];
    if (fileUploadLimits) {
      const providerLabel = fileUploadLimits.providerLabel || t("Selected provider");
      const limit = audioLimitLabel || fileUploadLimits.audioMaxLabel || t("unknown");
      details.push(
        fileUploadLimits.usesDirectProviderLimit
          ? t("{{provider}} accepts audio up to {{limit}}", { provider: providerLabel, limit })
          : t("{{provider}} processes files in the app up to {{limit}}", { provider: providerLabel, limit }),
      );
    }
    details.push(t("Video: only the extracted audio counts towards the transcription limit."));
    details.push(
      t("Files over {{threshold}} are optimized automatically before transcription.", { threshold: thresholdLabel }),
    );
    return { summary, details };
  }, [compressionThresholdBytes, fileUploadLimits, formatNumber, t]);
  const rawAudioLimit = fileUploadLimits?.rawAudioIngestMaxBytes || 2 * 1024 * 1024 * 1024;

  useEffect(() => {
    if (uploadSnapshot.completedFiles > 0) {
      void queryClient.invalidateQueries({
        predicate: (query) =>
          query.queryKey[0] === "/api/transcripts" && (query.queryKey[1] as { type?: string })?.type === "file",
      });
    }
  }, [uploadSnapshot.completedFiles, queryClient]);

  useTranscriptAutoRefresh({
    queryKey: transcriptsQueryKey,
  });

  const uploadFiles = useCallback(
    async (files: File[]) => {
      const selectedFiles = files.filter(Boolean);
      if (selectedFiles.length === 0) return;
      try {
        const result = await startFileUploadBatch(selectedFiles, {
          getServerProcessingText: (file) => inferServerProcessingText(file, compressionThresholdBytes),
        });

        if (result.failures.length > 0) {
          const firstFailure = result.failures[0];
          toast({
            title: result.responses.length > 0 ? translateNow("Some uploads failed") : translateNow("Upload failed"),
            description:
              result.responses.length > 0
                ? translateNow("{{started}} started, {{failed}} failed. {{file}}: {{error}}", {
                    started: formatNumberNow(result.responses.length),
                    failed: formatNumberNow(result.failures.length),
                    file: firstFailure.fileName,
                    error: translateNow(firstFailure.error),
                  })
                : translateNow("{{file}}: {{error}}", {
                    file: firstFailure.fileName,
                    error: translateNow(firstFailure.error),
                  }),
            variant: "destructive",
            duration: 7000,
          });
        } else {
          toast({
            title: selectedFiles.length === 1 ? translateNow("File uploaded") : translateNow("Files uploaded"),
            description:
              selectedFiles.length === 1
                ? translateNow("Transcription started…")
                : translateNow("{{count}} transcriptions started…", {
                    count: formatNumberNow(result.responses.length),
                  }),
            duration: 3000,
          });
        }

        queryClient.invalidateQueries({
          predicate: (query) =>
            query.queryKey[0] === "/api/transcripts" && (query.queryKey[1] as { type?: string })?.type === "file",
        });
      } catch (e: any) {
        toast({
          title: translateNow("Upload failed"),
          description: translateNow(String(e?.message || e)),
          duration: 5000,
        });
      }
    },
    [compressionThresholdBytes, queryClient, toast],
  );

  const deleteTranscript = useCallback(
    async (e: React.MouseEvent, id: string) => {
      e.stopPropagation(); // Prevent card click navigation
      if (deletingRef.current) return;

      deletingRef.current = id;
      setDeletingId(id);
      try {
        const res = await fetchWithTimeout(
          apiUrl(`/api/transcripts/${id}`),
          {
            method: "DELETE",
            credentials: "include",
          },
          15_000,
        );
        if (!res.ok) {
          const errData = (await res.json().catch(() => ({}))) as ApiMessageResponse;
          throw new Error(errData.message || res.statusText);
        }
        const deleted = (await res.json().catch(() => ({ success: true }))) as TranscriptDeleteResponse;
        if (deleted.success === false) {
          throw new Error(deleted.message || t("Delete failed"));
        }
        toast({
          title: t("Deleted"),
          description: t("Transcript removed successfully."),
          duration: 2000,
        });
        queryClient.invalidateQueries({
          predicate: (query) =>
            query.queryKey[0] === "/api/transcripts" && (query.queryKey[1] as { type?: string })?.type === "file",
        });
      } catch (e: any) {
        toast({
          title: t("Delete failed"),
          description: t(String(e?.message || e)),
          duration: 4000,
        });
      } finally {
        deletingRef.current = null;
        setDeletingId(null);
      }
    },
    [queryClient, t, toast],
  );

  const copyTranscript = useCallback(
    async (e: React.MouseEvent, id: string) => {
      e.stopPropagation();
      if (copyingRef.current) return;

      copyingRef.current = id;
      setCopyingId(id);
      try {
        // Fetch the full transcript content
        const res = await fetchWithTimeout(
          apiUrl(`/api/transcripts/${id}`),
          {
            credentials: "include",
          },
          15_000,
        );
        if (!res.ok) {
          throw new Error(res.statusText);
        }
        const data = (await res.json()) as TranscriptDetailResponse;
        const content = data?.content || "";
        if (!content) {
          throw new Error(t("No transcript content available"));
        }
        await navigator.clipboard.writeText(content);
        toast({
          title: t("Copied"),
          description: t("Transcript copied to clipboard."),
          duration: 2000,
        });
        // Show check mark briefly
        copyResetTimerRef.current = window.setTimeout(() => {
          copyingRef.current = null;
          copyResetTimerRef.current = null;
          setCopyingId(null);
        }, 1500);
      } catch (e: any) {
        toast({
          title: t("Copy failed"),
          description: t(String(e?.message || e)),
          duration: 4000,
        });
        copyingRef.current = null;
        setCopyingId(null);
      }
    },
    [t, toast],
  );

  const navigateToTranscript = useCallback(
    (id: string) => {
      setLocation(`/transcript/${id}`);
    },
    [setLocation],
  );

  const refreshAfterSummaryRetry = useCallback(
    (id: string) => {
      queryClient.invalidateQueries({ queryKey: ["/api/transcripts", id], exact: true });
      queryClient.invalidateQueries({
        predicate: (query) =>
          query.queryKey[0] === "/api/transcripts" && (query.queryKey[1] as { type?: string })?.type === "file",
      });
    },
    [queryClient],
  );

  // Preload TranscriptDetail page and data on hover for instant navigation
  const preloadTranscript = useCallback(
    (id: string) => {
      import("@/pages/TranscriptDetail");
      queryClient.prefetchQuery({ queryKey: ["/api/transcripts", id] });
    },
    [queryClient],
  );

  const onDrop = useCallback(
    (acceptedFiles: File[]) => {
      if (acceptedFiles.length > 0) {
        setDropError(null);
        uploadFiles(acceptedFiles);
      }
    },
    [uploadFiles],
  );

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    accept: {
      "audio/*": [".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac"],
      "video/*": [".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v", ".flv", ".wmv"],
    },
    multiple: true,
    validator: (file) =>
      // During drag-enter the browser may expose only a DataTransferItem;
      // its filename and size become available on drop.
      file.name && !VIDEO_EXTENSIONS.has(getFileExtension(file.name)) && file.size > rawAudioLimit
        ? { code: "file-too-large", message: "Audio file exceeds the raw import limit." }
        : null,
    onDropRejected: (rejections) => {
      const first = rejections[0];
      const reason =
        first?.errors?.[0]?.code === "file-too-large"
          ? "Audio file exceeds the raw import limit."
          : "Choose a supported audio or video file.";
      setDropError({ reason, fileName: first?.file?.name });
    },
  });

  // Upload progress and copy/delete state must not rebuild the entire history
  // or invalidate the virtualizer's row model.
  const { processingItems, completedItems } = useMemo(() => {
    const processingItems: TranscriptHistoryItem[] = [];
    const completedItems: TranscriptHistoryItem[] = [];
    for (const item of recentFromBackend) {
      (item.status === "processing" ? processingItems : completedItems).push(item);
    }
    return { processingItems, completedItems };
  }, [recentFromBackend]);

  const renderHistoryItem = useCallback(
    (item: TranscriptHistoryItem) => (
      <FileCard
        item={item}
        viewMode={viewMode}
        isDeleting={deletingId === item.id}
        isCopying={copyingId === item.id}
        onDelete={deleteTranscript}
        onCopy={copyTranscript}
        onSummaryRetryComplete={refreshAfterSummaryRetry}
        onNavigate={navigateToTranscript}
        onHover={preloadTranscript}
      />
    ),
    [
      copyingId,
      deleteTranscript,
      deletingId,
      copyTranscript,
      navigateToTranscript,
      preloadTranscript,
      refreshAfterSummaryRetry,
      viewMode,
    ],
  );

  return (
    <div
      className="app-page-shell transcription-page file-page px-4 py-5 md:px-6 md:py-6"
      data-page-accent="file"
      data-page-shell="file"
    >
      <PageIntro
        eyebrow={t("Media import")}
        title={t("File transcription")}
        description={t("Drop in audio or video; Scriber prepares, transcribes, and summarizes it in a structured way.")}
        accent="file"
      />

      {/* The picker remains usable while imports run in the shared queue. */}
      <div
        {...getRootProps({
          role: "button",
          "aria-label": t("Upload file for transcription"),
          "aria-describedby": "file-upload-formats file-upload-limits",
        })}
        className={`file-upload-shell mb-6 cursor-pointer group ${isDragActive ? "is-drag-active" : ""}`}
      >
        <input {...getInputProps()} />
        <div
          className={`file-upload-core flex items-center gap-5 p-6 md:p-7 ${isUploading ? "file-upload-compact" : "flex-col justify-center text-center"}`}
        >
          <div className="file-upload-mark flex h-14 w-14 shrink-0 items-center justify-center rounded-2xl">
            <UploadCloud className="h-7 w-7 stroke-[1.5px]" aria-hidden="true" />
          </div>
          <div className="min-w-0 space-y-1.5">
            <p className="font-heading text-[19px] font-semibold tracking-[-0.02em]">
              {isUploading ? t("Add more files") : t("Choose audio or video")}
            </p>
            <p id="file-upload-formats" className="text-xs leading-5 text-muted-foreground">
              {t("MP3, M4A, WAV, FLAC, MP4, MOV and WebM · multiple files supported")}
            </p>
            <p
              className={`file-upload-limits flex items-center gap-1 text-[11.5px] leading-5 text-muted-foreground ${isUploading ? "" : "justify-center"}`}
            >
              <span id="file-upload-limits">{uploadLimits.summary}</span>
              {/* The tooltip lives inside the dropzone; keep its clicks from opening the picker. */}
              <span
                className="inline-flex"
                onClick={(event) => event.stopPropagation()}
                onKeyDown={(event) => event.stopPropagation()}
              >
                <InfoTooltip compact label={t("Upload limits")}>
                  <ul className="file-upload-limits-details">
                    {uploadLimits.details.map((detail) => (
                      <li key={detail}>{detail}</li>
                    ))}
                  </ul>
                </InfoTooltip>
              </span>
            </p>
            <span className="file-upload-cta ui-cta mt-2 inline-flex h-10 items-center gap-2 px-4 text-xs font-semibold">
              {t("Browse files")}
              <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
            </span>
            <p className="text-[11px] text-muted-foreground">{t("or drop them anywhere in this panel")}</p>
          </div>
        </div>
      </div>
      {dropError && (
        <ErrorShake active trigger={dropError}>
          <div
            className="file-upload-error mb-5 break-words rounded-xl px-4 py-3 text-xs leading-5 text-destructive"
            role="alert"
          >
            {dropError.fileName
              ? t("{{file}}: {{error}}", { file: dropError.fileName, error: t(dropError.reason) })
              : t(dropError.reason)}
          </div>
        </ErrorShake>
      )}
      <FileImportQueue items={uploadSnapshot.items} />

      {/* Processing Queue */}
      {processingItems.length > 0 && (
        <section className="mb-7 space-y-3" aria-labelledby="file-processing-heading">
          <div className="flex flex-wrap items-end justify-between gap-2 px-1">
            <div>
              <div className="flex items-center gap-2.5">
                <h2 id="file-processing-heading" className="font-heading text-[17px] font-semibold tracking-[-0.015em]">
                  {t("Processing queue")}
                </h2>
                <span className="section-count-badge inline-flex h-6 min-w-6 items-center justify-center rounded-full px-2 text-[11px] font-semibold tabular-nums text-muted-foreground">
                  {formatNumber(processingItems.length)}
                </span>
              </div>
              <p className="mt-1 text-[12px] text-muted-foreground">
                {t("You can leave this page while Scriber continues.")}
              </p>
            </div>
          </div>
          {processingItems.map((item) => (
            <Card key={item.id} className="file-processing-card perf-scroll-item rounded-[20px] p-4">
              <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:gap-4">
                <div className="file-history-icon file-history-icon--media flex h-10 w-10 shrink-0 items-center justify-center rounded-[12px]">
                  <FileMediaIcon item={item} className="h-5 w-5 stroke-[1.65px]" />
                </div>
                <div className="flex-1 min-w-0">
                  <span className="block truncate font-heading text-[14px] font-medium">{item.title}</span>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {item.channel ? formatFileSizeLabel(t(item.channel), formatNumber) : t("Preparing your transcript")}
                  </p>
                </div>
                <div className="flex flex-wrap items-center gap-2 self-stretch sm:flex-nowrap sm:self-auto">
                  <Badge
                    variant="outline"
                    className="inline-flex h-9 shrink-0 items-center gap-1.5 border-primary/20 bg-primary/[0.06] px-2.5 text-ui-micro leading-none text-primary"
                  >
                    <span
                      className="h-1.5 w-1.5 shrink-0 rounded-full bg-current motion-safe:animate-pulse"
                      aria-hidden="true"
                    />
                    {localizedProcessingStep(item.step, "Processing", t, formatNumber)}
                  </Badge>
                  <TranscriptStopButton
                    transcriptId={item.id}
                    compact
                    onStop={() => {
                      void queryClient.invalidateQueries({ queryKey: transcriptsQueryKey });
                    }}
                  />
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-9 justify-center gap-2 text-muted-foreground hover:text-foreground"
                    type="button"
                    aria-label={t("View transcript {{title}}", { title: item.title })}
                    onClick={() => setLocation(`/transcript/${item.id}`)}
                  >
                    <ArrowRight className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                    {t("View")}
                  </Button>
                </div>
              </div>
            </Card>
          ))}
        </section>
      )}

      {/* History */}
      <div className="transcription-history space-y-4">
        <TranscriptionHistoryToolbar
          title={t("Recent files")}
          description={t("Search, copy, or reopen your imported transcripts.")}
          total={transcriptsQuery.total}
          itemLabel={transcriptsQuery.total === 1 ? t("file") : t("files")}
          searchValue={searchQuery}
          onSearchChange={setSearchQuery}
          searchPlaceholder={t("Search files…")}
          searchAriaLabel={t("Search file transcript history")}
          clearSearchLabel={t("Clear file search")}
          viewMode={viewMode}
          onViewModeChange={setViewMode}
        />
        <TranscriptHistoryPanel
          viewMode={viewMode}
          isLoading={transcriptsQuery.isLoading}
          isError={transcriptsQuery.isError}
          isEmpty={completedItems.length === 0 && !transcriptsQuery.hasNextPage && !transcriptsQuery.isFetchingNextPage}
          searchTerm={debouncedSearch}
          errorTitle={t("Could not load file transcripts")}
          errorDescription={t("Please retry loading your file history.")}
          onRetry={() => void transcriptsQuery.refetch()}
          noMatchesState={
            <p className="py-8 text-center text-muted-foreground">
              {t("No files match “{{query}}”", { query: debouncedSearch })}
            </p>
          }
          emptyState={<EmptyState type="file" />}
        >
          <VirtualTranscriptHistory
            items={completedItems}
            viewMode={viewMode}
            getItemKey={transcriptHistoryItemKey}
            hasMore={transcriptsQuery.hasNextPage}
            isLoadingMore={transcriptsQuery.isFetchingNextPage}
            onLoadMore={transcriptsQuery.fetchNextPage}
            renderItem={renderHistoryItem}
          />
        </TranscriptHistoryPanel>
      </div>
    </div>
  );
}

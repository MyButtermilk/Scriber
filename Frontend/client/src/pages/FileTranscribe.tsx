import { useCallback, useState, useEffect, memo, useMemo, useRef, useSyncExternalStore } from "react";
import { useDropzone } from "react-dropzone";
import { AlertCircle, UploadCloud, FileAudio, CheckCircle2, Loader2, XCircle, Square, ArrowRight } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { useLocation } from "wouter";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { apiUrl } from "@/lib/backend";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";
import { useToast } from "@/hooks/use-toast";
import { EmptyState } from "@/components/ui/empty-state";
import { SkeletonList } from "@/components/ui/skeleton-card";
import { QueryErrorState } from "@/components/ui/query-error-state";
import { useTranscriptAutoRefresh } from "@/hooks/use-transcript-auto-refresh";
import { useUrlQueryState } from "@/hooks/use-url-query-state";
import { DeleteActionButton } from "@/components/ui/delete-action-button";
import { CopyActionButton } from "@/components/ui/copy-action-button";
import { PageIntro } from "@/components/page-intro";
import { TranscriptionHistoryToolbar } from "@/components/transcription-history-toolbar";
import { TranscriptSummaryRetryButton } from "@/components/transcript-summary-retry-button";
import { VirtualTranscriptHistory } from "@/components/virtual-transcript-history";
import { transcriptHistoryQueryKey, useTranscriptHistoryQuery } from "@/hooks/use-transcript-history-query";
import {
  getFileUploadSnapshot,
  isFileUploadActive,
  startFileUploadBatch,
  subscribeFileUpload,
} from "@/lib/file-upload-store";
import type {
  ApiMessageResponse,
  SettingsResponse,
  TranscriptDeleteResponse,
  TranscriptDetailResponse,
  TranscriptHistoryItem,
} from "@/lib/api-types";

const VIEW_MODE_STORAGE_KEY = "scriber:view-mode";
const DEFAULT_COMPRESSION_THRESHOLD_BYTES = 50 * 1024 * 1024;
const VIDEO_EXTENSIONS = new Set([".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v"]);

function getFileExtension(fileName: string): string {
  const dotIndex = fileName.lastIndexOf(".");
  return dotIndex >= 0 ? fileName.slice(dotIndex).toLowerCase() : "";
}

function inferServerProcessingLabel(file: File, compressionThresholdBytes: number): string {
  const ext = getFileExtension(file.name);
  if (VIDEO_EXTENSIONS.has(ext)) {
    return `Extracting audio from ${file.name}...`;
  }
  if (file.size > compressionThresholdBytes) {
    return `Compressing ${file.name}...`;
  }
  return `Preparing ${file.name}...`;
}

type FileHistoryStatus = "processing" | "failed" | "summary_failed" | "stopped" | "ready";

function fileHistoryStatus(item: TranscriptHistoryItem): FileHistoryStatus {
  if (item.summaryStatus === "pending" || item.status === "processing") return "processing";
  if (item.status === "failed") return "failed";
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
  const deletingClasses = isDeleting
    ? "pointer-events-none opacity-[0.55] scale-[0.985]"
    : "opacity-100 scale-100";
  const historyStatus = fileHistoryStatus(item);

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
              <div className={`file-history-icon flex h-10 w-10 items-center justify-center rounded-[12px] ${historyStatus === 'failed' || historyStatus === 'summary_failed'
                ? 'bg-red-50 dark:bg-red-900/20 text-red-600'
                : historyStatus === 'processing'
                  ? 'bg-blue-50 dark:bg-blue-900/20 text-blue-600'
                  : historyStatus === 'stopped'
                  ? 'bg-yellow-50 dark:bg-yellow-900/20 text-yellow-600'
                  : 'bg-gradient-to-br from-green-500/20 to-green-500/5 text-green-600'
                }`}>
                {historyStatus === 'failed' ? <XCircle className="w-5 h-5" /> : historyStatus === 'summary_failed' ? <AlertCircle className="w-5 h-5" /> : historyStatus === 'processing' ? <Loader2 className="w-5 h-5 animate-spin" /> : historyStatus === 'stopped' ? <Square className="w-5 h-5" /> : <FileAudio className="w-5 h-5" />}
              </div>
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
                  {item.channel && <span>{item.channel}</span>}
                  {item.channel && <span>•</span>}
                  <span>{item.duration}</span>
                  <span>•</span>
                  <span>{item.date}</span>
                </div>
              </div>
            </div>
            <div className="flex flex-wrap items-center justify-end gap-2">
              {historyStatus === 'processing' ? (
                <Badge variant="outline" className="text-blue-600 border-blue-200 bg-blue-50 text-[10px] flex items-center gap-1">
                  <Loader2 className="w-3 h-3 animate-spin" />
                  {item.summaryStatus === "pending" ? "Summarizing…" : item.step || "Processing"}
                </Badge>
              ) : historyStatus === 'failed' ? (
                <Badge variant="outline" className="text-red-600 border-red-200 bg-red-50 text-[10px]">Failed</Badge>
              ) : historyStatus === "summary_failed" ? (
                <TranscriptSummaryRetryButton
                  transcriptId={item.id}
                  transcriptTitle={item.title}
                  onComplete={onSummaryRetryComplete}
                />
              ) : historyStatus === 'stopped' ? (
                <Badge variant="outline" className="text-yellow-600 border-yellow-200 bg-yellow-50 text-[10px]">Stopped</Badge>
              ) : (
                <div className="hidden sm:flex items-center gap-1 text-xs font-medium text-green-600 bg-green-50 px-2 py-1 rounded-full">
                  <CheckCircle2 className="w-3 h-3" />
                  Ready
                </div>
              )}
              <CopyActionButton
                onClick={(e) => onCopy(e, item.id)}
                disabled={isCopying}
                copied={isCopying}
                title="Copy transcript"
                ariaLabel={`Copy transcript ${item.title}`}
                className="opacity-100 md:opacity-0 md:group-hover:opacity-100 md:group-focus-within:opacity-100 transition-opacity"
              />
              <DeleteActionButton
                onClick={(e) => onDelete(e, item.id)}
                disabled={isDeleting}
                loading={isDeleting}
                title="Delete transcript"
                ariaLabel={`Delete transcript ${item.title}`}
                className="opacity-100 md:opacity-0 md:group-hover:opacity-100 md:group-focus-within:opacity-100 transition-opacity"
              />
            </div>
          </div>
        ) : (
          // Grid view
          <div className="flex flex-col h-full">
            <div className="flex items-start justify-between mb-3">
              <div className={`file-history-icon flex h-12 w-12 items-center justify-center rounded-[13px] ${historyStatus === 'failed' || historyStatus === 'summary_failed'
                ? 'bg-red-50 dark:bg-red-900/20 text-red-600'
                : historyStatus === 'processing'
                  ? 'bg-blue-50 dark:bg-blue-900/20 text-blue-600'
                  : historyStatus === 'stopped'
                  ? 'bg-yellow-50 dark:bg-yellow-900/20 text-yellow-600'
                  : 'bg-gradient-to-br from-green-500/20 to-green-500/5 text-green-600'
                }`}>
                {historyStatus === 'failed' ? <XCircle className="w-6 h-6" /> : historyStatus === 'summary_failed' ? <AlertCircle className="w-6 h-6" /> : historyStatus === 'processing' ? <Loader2 className="w-6 h-6 animate-spin" /> : historyStatus === 'stopped' ? <Square className="w-6 h-6" /> : <FileAudio className="w-6 h-6" />}
              </div>
              <div className="flex items-center gap-1">
                {historyStatus === 'processing' ? (
                  <Badge variant="outline" className="text-blue-600 border-blue-200 bg-blue-50 text-[10px] flex items-center gap-1">
                    <Loader2 className="w-3 h-3 animate-spin" />
                    {item.summaryStatus === "pending" ? "Summarizing…" : null}
                  </Badge>
                ) : historyStatus === 'failed' ? (
                  <Badge variant="outline" className="text-red-600 border-red-200 bg-red-50 text-[10px]">Failed</Badge>
                ) : historyStatus === "summary_failed" ? (
                  <TranscriptSummaryRetryButton
                    transcriptId={item.id}
                    transcriptTitle={item.title}
                    onComplete={onSummaryRetryComplete}
                  />
                ) : historyStatus === 'stopped' ? (
                  <Badge variant="outline" className="text-yellow-600 border-yellow-200 bg-yellow-50 text-[10px]">Stopped</Badge>
                ) : (
                  <div className="flex items-center gap-1 rounded-full bg-green-50 px-2 py-1 text-[10px] font-medium text-green-600 dark:bg-green-950/40 dark:text-green-300">
                    <CheckCircle2 className="w-3 h-3" />
                    Ready
                  </div>
                )}
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
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground mt-auto">
              <span>{item.duration}</span>
              <span>•</span>
              <span>{item.date}</span>
            </div>
            <div className="flex items-center justify-end mt-2 gap-1">
              <CopyActionButton
                onClick={(e) => onCopy(e, item.id)}
                disabled={isCopying}
                copied={isCopying}
                title="Copy transcript"
                ariaLabel={`Copy transcript ${item.title}`}
                size="sm"
                className="opacity-100 md:opacity-0 md:group-hover:opacity-100 md:group-focus-within:opacity-100 transition-opacity"
              />
              <DeleteActionButton
                onClick={(e) => onDelete(e, item.id)}
                disabled={isDeleting}
                loading={isDeleting}
                title="Delete transcript"
                ariaLabel={`Delete transcript ${item.title}`}
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
  const [location, setLocation] = useLocation();
  const { toast } = useToast();
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [copyingId, setCopyingId] = useState<string | null>(null);
  const [dropError, setDropError] = useState("");
  const deletingRef = useRef<string | null>(null);
  const copyingRef = useRef<string | null>(null);
  const copyResetTimerRef = useRef<number | null>(null);
  const uploadSnapshot = useSyncExternalStore(
    subscribeFileUpload,
    getFileUploadSnapshot,
    getFileUploadSnapshot,
  );
  const isUploading = uploadSnapshot.status === "uploading" || uploadSnapshot.status === "server_processing";
  const uploadProgress = uploadSnapshot.progress;
  const uploadingFileName = uploadSnapshot.fileName;
  const uploadStatusText = uploadSnapshot.statusText;
  const uploadQueueItems = uploadSnapshot.items;
  const uploadTotalFiles = uploadSnapshot.totalFiles;
  const uploadFinishedFiles = uploadSnapshot.completedFiles + uploadSnapshot.failedFiles;
  const queryClient = useQueryClient();
  const getInitialViewMode = () => {
    if (typeof window === "undefined") return "list" as const;
    const stored = window.localStorage.getItem(VIEW_MODE_STORAGE_KEY);
    if (stored === "list" || stored === "grid") return stored;
    return "list" as const;
  };
  const initialViewMode = getInitialViewMode();
  const [viewMode, setViewMode] = useUrlQueryState<"list" | "grid">("view", initialViewMode, {
    parse: (raw) => (raw === "list" || raw === "grid" ? raw : initialViewMode),
  });

  // Search state
  const [searchQuery, setSearchQuery] = useUrlQueryState("q", "", {
    parse: (raw) => raw ?? "",
    serialize: (value) => {
      const trimmed = value.trim();
      return trimmed ? trimmed : null;
    },
    syncDelayMs: 250,
  });
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const transcriptsQueryKey = useMemo(
    () => transcriptHistoryQueryKey("file", debouncedSearch),
    [debouncedSearch],
  );

  // Debounce search
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(searchQuery), 300);
    return () => clearTimeout(timer);
  }, [searchQuery]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(VIEW_MODE_STORAGE_KEY, viewMode);
  }, [viewMode]);

  useEffect(() => () => {
    if (copyResetTimerRef.current !== null) {
      window.clearTimeout(copyResetTimerRef.current);
    }
  }, []);

  const transcriptsQuery = useTranscriptHistoryQuery<TranscriptHistoryItem>({ type: "file", q: debouncedSearch });
  const recentFromBackend = transcriptsQuery.items;
  const settingsQuery = useQuery<SettingsResponse>({
    queryKey: ["/api/settings"],
    queryFn: async ({ signal }) => {
      const res = await fetchWithTimeout(
        apiUrl("/api/settings"),
        { credentials: "include", signal },
        10_000,
      );
      if (!res.ok) throw new Error("Failed to load settings");
      return (await res.json()) as SettingsResponse;
    },
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });
  const fileUploadLimits = settingsQuery.data?.fileUploadLimits;
  const compressionThresholdBytes =
    Number(fileUploadLimits?.compressionThresholdBytes) || DEFAULT_COMPRESSION_THRESHOLD_BYTES;
  const uploadHint = useMemo(() => {
    if (!fileUploadLimits) {
      return "Audio and video up to 2GB · files over 50MB are optimized automatically";
    }

    const providerLabel = fileUploadLimits.providerLabel || "Selected provider";
    const compressionThresholdLabel = fileUploadLimits.compressionThresholdLabel || "50MB";
    const audioLimitLabel = fileUploadLimits.audioMaxLabel || "unknown";
    const videoLimitLabel = fileUploadLimits.videoMaxLabel || "2GB";

    const audioHint = fileUploadLimits.usesDirectProviderLimit
      ? `${providerLabel} accepts audio up to ${audioLimitLabel}`
      : `${providerLabel} processes files in-app up to ${audioLimitLabel}`;

    return `${audioHint} · video up to ${videoLimitLabel} · files over ${compressionThresholdLabel} are optimized automatically`;
  }, [fileUploadLimits]);
  const maxUploadBytes = useMemo(
    () => Math.max(
      Number(fileUploadLimits?.rawAudioIngestMaxBytes) || 0,
      Number(fileUploadLimits?.videoMaxBytes) || 0,
      2 * 1024 * 1024 * 1024,
    ),
    [fileUploadLimits],
  );

  useTranscriptAutoRefresh({
    queryKey: transcriptsQueryKey,
  });

  const uploadFiles = useCallback(async (files: File[]) => {
    const selectedFiles = files.filter(Boolean);
    if (selectedFiles.length === 0) return;
    try {
      const result = await startFileUploadBatch(selectedFiles, {
        getServerProcessingLabel: (file) => inferServerProcessingLabel(file, compressionThresholdBytes),
      });

      if (result.failures.length > 0) {
        const firstFailure = result.failures[0];
        toast({
          title: result.responses.length > 0 ? "Some uploads failed" : "Upload failed",
          description:
            result.responses.length > 0
              ? `${result.responses.length} started, ${result.failures.length} failed. ${firstFailure.fileName}: ${firstFailure.error}`
              : `${firstFailure.fileName}: ${firstFailure.error}`,
          variant: "destructive",
          duration: 7000,
        });
      } else {
        toast({
          title: selectedFiles.length === 1 ? "File uploaded" : "Files uploaded",
          description:
            selectedFiles.length === 1
              ? "Transcription started..."
              : `${result.responses.length} transcriptions started...`,
          duration: 3000,
        });
      }

      queryClient.invalidateQueries({
        predicate: (query) =>
          query.queryKey[0] === "/api/transcripts" &&
          (query.queryKey[1] as { type?: string })?.type === "file",
      });

      // Stay out of the user's way if they intentionally switched tabs while
      // the long upload/extraction request was still running.
      const currentPath = typeof window !== "undefined" ? window.location.pathname : location;
      if (selectedFiles.length === 1 && result.responses[0]?.id && currentPath === "/file") {
        setLocation(`/transcript/${result.responses[0].id}`);
      }
    } catch (e: any) {
      toast({
        title: "Upload failed",
        description: String(e?.message || e),
        duration: 5000,
      });
    }
  }, [compressionThresholdBytes, location, queryClient, setLocation, toast]);

  const deleteTranscript = useCallback(async (e: React.MouseEvent, id: string) => {
    e.stopPropagation(); // Prevent card click navigation
    if (deletingRef.current) return;

    deletingRef.current = id;
    setDeletingId(id);
    try {
      const res = await fetchWithTimeout(apiUrl(`/api/transcripts/${id}`), {
        method: "DELETE",
        credentials: "include",
      }, 15_000);
      if (!res.ok) {
        const errData = (await res.json().catch(() => ({}))) as ApiMessageResponse;
        throw new Error(errData.message || res.statusText);
      }
      const deleted = (await res.json().catch(() => ({ success: true }))) as TranscriptDeleteResponse;
      if (deleted.success === false) {
        throw new Error(deleted.message || "Delete failed");
      }
      toast({
        title: "Deleted",
        description: "Transcript removed successfully.",
        duration: 2000,
      });
      queryClient.invalidateQueries({
        predicate: (query) =>
          query.queryKey[0] === "/api/transcripts" &&
          (query.queryKey[1] as { type?: string })?.type === "file",
      });
    } catch (e: any) {
      toast({
        title: "Delete failed",
        description: String(e?.message || e),
        duration: 4000,
      });
    } finally {
      deletingRef.current = null;
      setDeletingId(null);
    }
  }, [queryClient, toast]);

  const copyTranscript = useCallback(async (e: React.MouseEvent, id: string) => {
    e.stopPropagation();
    if (copyingRef.current) return;

    copyingRef.current = id;
    setCopyingId(id);
    try {
      // Fetch the full transcript content
      const res = await fetchWithTimeout(apiUrl(`/api/transcripts/${id}`), {
        credentials: "include",
      }, 15_000);
      if (!res.ok) {
        throw new Error(res.statusText);
      }
      const data = (await res.json()) as TranscriptDetailResponse;
      const content = data?.content || "";
      if (!content) {
        throw new Error("No transcript content available");
      }
      await navigator.clipboard.writeText(content);
      toast({
        title: "Copied",
        description: "Transcript copied to clipboard.",
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
        title: "Copy failed",
        description: String(e?.message || e),
        duration: 4000,
      });
      copyingRef.current = null;
      setCopyingId(null);
    }
  }, [toast]);

  const navigateToTranscript = useCallback((id: string) => {
    setLocation(`/transcript/${id}`);
  }, [setLocation]);

  const refreshAfterSummaryRetry = useCallback((id: string) => {
    queryClient.invalidateQueries({ queryKey: ["/api/transcripts", id], exact: true });
    queryClient.invalidateQueries({
      predicate: (query) =>
        query.queryKey[0] === "/api/transcripts" &&
        (query.queryKey[1] as { type?: string })?.type === "file",
    });
  }, [queryClient]);

  // Preload TranscriptDetail page and data on hover for instant navigation
  const preloadTranscript = useCallback((id: string) => {
    import("@/pages/TranscriptDetail");
    queryClient.prefetchQuery({ queryKey: ["/api/transcripts", id] });
  }, [queryClient]);

  const onDrop = useCallback((acceptedFiles: File[]) => {
    if (acceptedFiles.length > 0 && !isFileUploadActive()) {
      setDropError("");
      uploadFiles(acceptedFiles);
    }
  }, [uploadFiles]);

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    accept: {
      "audio/*": [".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac"],
      "video/*": [".mp4", ".mov", ".webm", ".avi", ".mkv", ".m4v"],
    },
    multiple: true,
    maxSize: maxUploadBytes,
    disabled: isUploading,
    onDropRejected: (rejections) => {
      const first = rejections[0];
      const reason = first?.errors?.[0]?.code === "file-too-large"
        ? "This file is larger than the current 2GB ingest limit."
        : "Choose a supported audio or video file.";
      setDropError(first?.file?.name ? `${first.file.name}: ${reason}` : reason);
    },
  });

  // Separate processing items from completed
  const processingItems = recentFromBackend.filter((t) => t.status === "processing");
  const completedItems = recentFromBackend.filter((t) => t.status !== "processing");

  return (
    <div className="app-page-shell transcription-page file-page px-4 py-5 md:px-6 md:py-6" data-page-shell="file">
      <PageIntro
        eyebrow="Media import · 04"
        title="File transcription"
        description="Drop in audio or video; Scriber prepares, transcribes, and organizes it."
        sticky={false}
      />

      {/* Import workbench */}
      <div
        {...getRootProps({
          role: "button",
          "aria-label": "Upload file for transcription",
          "aria-describedby": "file-upload-formats file-upload-limits",
          "aria-busy": isUploading,
        })}
        className={`file-upload-shell mb-7 cursor-pointer group
          ${isDragActive ? 'is-drag-active' : ''}
          ${isUploading ? 'is-uploading' : ''}
        `}
      >
        <div className="file-upload-core flex flex-col items-center justify-center gap-4 p-6 text-center md:p-8">
        <input {...getInputProps()} />
        <div className="file-upload-mark flex h-[72px] w-[72px] items-center justify-center rounded-full">
          {isUploading ? (
            <Loader2 className="h-8 w-8 animate-spin text-primary" />
          ) : (
            <UploadCloud className={`h-8 w-8 stroke-[1.45px] ${isDragActive ? 'text-primary' : 'text-muted-foreground'}`} />
          )}
        </div>
        <div className="space-y-1">
          {isUploading ? (
            <>
              <p className="text-pretty font-heading text-[17px] font-semibold">{uploadStatusText || `Uploading ${uploadingFileName}...`}</p>
              <Progress value={uploadProgress} className="mx-auto mt-3 h-2 w-[min(18rem,70vw)]" />
              {uploadTotalFiles > 1 && (
                <p className="text-xs text-muted-foreground mt-1">
                  {uploadFinishedFiles} of {uploadTotalFiles} files prepared
                </p>
              )}
            </>
          ) : (
            <>
              <p className="font-heading text-[19px] font-semibold tracking-[-0.02em]">Choose audio or video</p>
              <p id="file-upload-formats" className="text-[12px] leading-5 text-muted-foreground">
                MP3, M4A, WAV, FLAC, MP4, MOV and WebM · multiple files supported
              </p>
              <span className="file-upload-cta mt-2 inline-flex h-10 items-center gap-2 rounded-[11px] px-4 text-[12px] font-semibold text-primary">
                <UploadCloud className="h-4 w-4" aria-hidden="true" />
                Browse files
              </span>
              <p className="text-[11px] text-muted-foreground">or drop them anywhere in this panel</p>
            </>
          )}
        </div>
        {dropError && !isUploading ? (
          <div className="file-upload-error max-w-xl rounded-[12px] px-3 py-2 text-left text-[12px] leading-5 text-destructive" role="alert">
            {dropError}
          </div>
        ) : null}
        {isUploading && uploadQueueItems.length > 1 && (
          <div className="w-full max-w-md space-y-2 text-left">
            {uploadQueueItems.map((item) => (
              <div key={item.id} className="flex items-center gap-3 text-xs">
                <span
                  className={`h-2 w-2 rounded-full shrink-0 ${
                    item.status === "failed"
                      ? "bg-red-500"
                      : item.status === "completed"
                      ? "bg-green-500"
                      : item.status === "queued"
                      ? "bg-muted-foreground/40"
                      : "bg-primary"
                  }`}
                />
                <span className="min-w-0 flex-1 truncate text-foreground">{item.fileName}</span>
                <span className="text-muted-foreground tabular-nums">
                  {item.status === "queued" ? "Queued" : item.status === "failed" ? "Failed" : `${item.progress}%`}
                </span>
              </div>
            ))}
          </div>
        )}
        {!isUploading && (
          <p id="file-upload-limits" className="file-upload-formats mt-1 max-w-3xl text-pretty text-[10.5px] leading-5 text-muted-foreground">
            {uploadHint}
          </p>
        )}
        </div>
      </div>

      {/* Processing Queue */}
      {processingItems.length > 0 && (
        <section className="mb-7 space-y-3" aria-labelledby="file-processing-heading">
          <div className="flex flex-wrap items-end justify-between gap-2 px-1">
            <div>
              <div className="flex items-center gap-2.5">
                <h2 id="file-processing-heading" className="font-heading text-[17px] font-semibold tracking-[-0.015em]">Processing queue</h2>
                <span className="transcription-history-count inline-flex h-6 min-w-6 items-center justify-center rounded-[8px] px-2 font-mono text-[10.5px] font-semibold tabular-nums text-muted-foreground">
                  {processingItems.length}
                </span>
              </div>
              <p className="mt-1 text-[12px] text-muted-foreground">You can leave this page while Scriber continues.</p>
            </div>
          </div>
          {processingItems.map((item) => (
            <Card key={item.id} className="file-processing-card perf-scroll-item rounded-[20px] p-4">
              <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:gap-4">
                <div className="file-history-icon flex h-10 w-10 shrink-0 items-center justify-center rounded-[12px] text-primary">
                  <FileAudio className="h-5 w-5" aria-hidden="true" />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="min-w-0 flex-1 truncate font-heading text-[14px] font-medium">{item.title}</span>
                    <Badge variant="outline" className="flex shrink-0 items-center gap-1 border-primary/20 bg-primary/[0.06] text-[10px] text-primary">
                      <Loader2 className="h-3 w-3 animate-spin" aria-hidden="true" />
                      {item.step || "Processing"}
                    </Badge>
                  </div>
                  <p className="mt-1 text-xs text-muted-foreground">{item.channel || "Preparing your transcript"}</p>
                </div>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-9 justify-center gap-2 self-stretch text-muted-foreground hover:text-foreground sm:self-auto"
                  type="button"
                  aria-label={`View transcript ${item.title}`}
                  onClick={() => setLocation(`/transcript/${item.id}`)}
                >
                  View
                  <ArrowRight className="h-3.5 w-3.5" aria-hidden="true" />
                </Button>
              </div>
            </Card>
          ))}
        </section>
      )}

      {/* History */}
      <div className="transcription-history space-y-4">
        <TranscriptionHistoryToolbar
          title="Recent files"
          description="Search, copy, or reopen your imported transcripts."
          total={transcriptsQuery.total}
          itemLabel={transcriptsQuery.total === 1 ? "file" : "files"}
          searchValue={searchQuery}
          onSearchChange={setSearchQuery}
          searchPlaceholder="Search files..."
          searchAriaLabel="Search file transcript history"
          clearSearchLabel="Clear file search"
          viewMode={viewMode}
          onViewModeChange={setViewMode}
        />
        {transcriptsQuery.isLoading ? (
          <SkeletonList count={3} variant={viewMode} />
        ) : transcriptsQuery.isError ? (
          <QueryErrorState
            title="Could not load file transcripts"
            description="Please retry loading your file history."
            onRetry={() => transcriptsQuery.refetch()}
          />
        ) : completedItems.length === 0 && !transcriptsQuery.hasNextPage && !transcriptsQuery.isFetchingNextPage ? (
          debouncedSearch ? (
            <p className="text-center text-muted-foreground py-8">No files match "{debouncedSearch}"</p>
          ) : (
            <EmptyState type="file" />
          )
        ) : (
          <VirtualTranscriptHistory
            items={completedItems}
            viewMode={viewMode}
            getItemKey={(item) => item.id}
            hasMore={transcriptsQuery.hasNextPage}
            isLoadingMore={transcriptsQuery.isFetchingNextPage}
            onLoadMore={() => transcriptsQuery.fetchNextPage()}
            renderItem={(item) => (
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
            )}
          />
        )}
      </div>
    </div>
  );
}


import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation } from "wouter";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  AlertTriangle,
  CalendarClock,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CirclePause,
  CirclePlay,
  Download,
  FileUp,
  FileText,
  FolderOpen,
  Headphones,
  Loader2,
  Mail,
  Mic2,
  MonitorSpeaker,
  NotebookPen,
  Paperclip,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  ShieldCheck,
  Sparkles,
  Square,
  Trash2,
  Undo2,
  Users,
  Volume2,
  Waves,
  X,
} from "lucide-react";
import { apiUrl } from "@/lib/backend";
import { apiRequest, OUTLOOK_SYNC_REQUEST_TIMEOUT_MS } from "@/lib/queryClient";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";
import {
  meetingExportFolderName,
  openMeetingExport,
  revealMeetingExport,
  saveMeetingExport,
  type MeetingExportResult,
} from "@/lib/meeting-export";
import {
  meetingEmailDraftPath,
  type MeetingEmailDraftAttachment,
} from "@/lib/meeting-export-utils";
import {
  calculateMeetingElapsedMs,
  captureMeetingPlaybackRequest,
  formatMeetingOffset,
  meetingCheckpointFreshness,
  meetingPlaybackOriginMs,
  meetingSpeakerSampleWindow,
  meetingTimeToAssetTimeSeconds,
  MEETING_SPEAKER_SAMPLE_MIN_MS,
  type MeetingPlaybackRequest,
} from "@/lib/meeting-playback";
import { localizeMeetingErrorMessage } from "@/lib/meeting-error-message";
import { useSharedWebSocket, useWebSocketContext, type ScriberWebSocketMessage } from "@/contexts/WebSocketContext";
import { useToast } from "@/hooks/use-toast";
import { useI18n, type TranslationValues } from "@/i18n";
import {
  applyMeetingActionItem,
  applyMeetingCheckpointEvent,
  applyMeetingNoteEvent,
  applyMeetingProgressEvent,
  applyMeetingSegmentEvent,
  applyMeetingSpeakerName,
  applyMeetingSpeakerProfileSplit,
  applyMeetingSummaryEvent,
  applyMeetingTranscriptEditedEvent,
  isMeetingWebSocketReconnect,
  isNewMeetingSetupEnabled,
  meetingDetailRefetchInterval,
  mergeMeetingProcessingProgress,
  MEETING_HISTORY_QUERY_KEY,
  refreshMeetingCapabilities,
  refreshMeetingCollections,
  refreshMeetingDetail,
} from "@/lib/meeting-cache";
import {
  applyMeetingImportProgressEvent,
  MEETING_IMPORTS_QUERY_KEY,
  mergeMeetingImportProgress,
  type MeetingImportProgressView,
  upsertMeetingImportJob,
} from "@/lib/meeting-import-cache";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { PageIntro } from "@/components/page-intro";
import { OutlookMeetingPicker } from "@/components/meeting/OutlookMeetingPicker";
import {
  SpeakerAttendeeAssignments,
  type MeetingSpeakerAssignmentFocusRequest,
} from "@/components/meeting/SpeakerAttendeeAssignments";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import type {
  MeetingCapabilities,
  MeetingActionItem,
  MeetingDetail,
  MeetingAudioDevicesResponse,
  MeetingDetectionResponse,
  MeetingDeviceTestResponse,
  MeetingImportJob,
  MeetingImportsResponse,
  MeetingNote,
  MeetingProviderProfile,
  MeetingProcessingProgress,
  MeetingProcessingComponent,
  MeetingProfilesResponse,
  MeetingSegment,
  MeetingSpeakerAssignmentsResponse,
  MeetingState,
  MeetingSummary,
  MeetingsResponse,
  OutlookCalendarEvent,
  OutlookCalendarEventsResponse,
  OutlookCalendarStatus,
  OutlookCalendarSyncResponse,
  SpeakerModelStatus,
  SpeakerProfilesResponse,
} from "@/lib/api-types";

const OPEN_STATES = new Set<MeetingState>(["starting", "recording", "paused", "stopping", "finalizing", "analyzing"]);
const TERMINAL_MEETING_STATES = new Set<MeetingState>([
  "ready", "capture_failed", "finalization_failed", "analysis_failed", "interrupted", "discarded",
]);
type MeetingWorkspaceView = "overview" | "transcript" | "decisions" | "actions" | "questions" | "notes" | "chat";
type MeetingReprocessMode = "speaker_identity" | "full_transcript";
type Translate = (source: string, values?: TranslationValues) => string;
type FormatDate = (value: Date | number | string, options?: Intl.DateTimeFormatOptions) => string;
type FormatNumber = (value: number, options?: Intl.NumberFormatOptions) => string;
const MEETING_WORKSPACE_VIEWS: ReadonlyArray<readonly [MeetingWorkspaceView, string]> = [
  ["overview", "Overview"], ["transcript", "Transcript"], ["decisions", "Decisions"],
  ["actions", "Action items"], ["questions", "Open questions"],
  ["notes", "Notes"], ["chat", "Ask meeting"],
];

function processingComponentLabel(component: MeetingProcessingComponent | undefined, t: Translate): string {
  if (!component) return t("Not recorded for this meeting");
  if (!component.used) return t("Not used");
  return [component.engine, component.model].filter(Boolean).join(" · ") || t("Used");
}

function processingComponentModeLabel(component: MeetingProcessingComponent | undefined, t: Translate, formatNumber: FormatNumber): string {
  if (!component) return "";
  const labels: Record<string, string> = {
    local_fallback: "Local fallback after transcription",
    provider_native: "Included by the transcription provider",
    audio_segmentation: "Used to find speech in the recording",
    live_preview_boundaries: "Used for live-preview turn boundaries",
    not_requested: "Not requested for this meeting",
    failed_or_unavailable: "Requested, but failed or was unavailable",
    ready_no_completed_turns: "Ready, but no complete turn required analysis",
    no_live_session_evidence: "No completed live-session evidence was recorded",
  };
  const label = t(labels[component.mode] || component.mode.replaceAll("_", " "));
  if (component.analysisCount) return `${label} · ${t(component.analysisCount === 1 ? "{{count}} analysis" : "{{count}} analyses", { count: formatNumber(component.analysisCount) })}`;
  if (component.failureCount) return `${label} · ${t(component.failureCount === 1 ? "{{count}} failure" : "{{count}} failures", { count: formatNumber(component.failureCount) })}`;
  return component.mode === "not_used" ? "" : label;
}

function stateLabel(state: MeetingState, t: Translate): string {
  const labels: Record<MeetingState, string> = {
    starting: "Starting",
    recording: "Recording",
    paused: "Paused",
    stopping: "Saving",
    finalizing: "Creating transcript",
    analyzing: "Creating meeting brief",
    ready: "Ready",
    capture_failed: "Recording stopped",
    finalization_failed: "Transcript needs attention",
    analysis_failed: "Meeting brief needs attention",
    interrupted: "Recording interrupted",
    discarded: "Discarded",
  };
  return t(labels[state]);
}

function stateTone(state: MeetingState): string {
  if (state === "recording") return "border-red-300/60 bg-red-500/10 text-red-700 dark:text-red-300";
  if (["capture_failed", "finalization_failed", "analysis_failed", "interrupted"].includes(state)) {
    return "border-amber-300/60 bg-amber-500/10 text-amber-800 dark:text-amber-200";
  }
  if (state === "ready") return "border-emerald-300/60 bg-emerald-500/10 text-emerald-800 dark:text-emerald-200";
  return "border-blue-300/60 bg-blue-500/10 text-blue-800 dark:text-blue-200";
}

function formatMoment(value: string | null, formatDate: FormatDate): string {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return formatDate(date, { dateStyle: "medium", timeStyle: "short" });
}

function localCalendarDate(value = new Date()): string {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function localCalendarDayWindow(value = new Date()): { start: string; end: string } {
  const start = new Date(value.getFullYear(), value.getMonth(), value.getDate());
  const end = new Date(value.getFullYear(), value.getMonth(), value.getDate() + 1);
  return { start: start.toISOString(), end: end.toISOString() };
}

const formatOffset = formatMeetingOffset;

function formatImportDuration(seconds: number | null, t: Translate): string {
  if (seconds == null || !Number.isFinite(seconds)) return t("Duration checked during import");
  const rounded = Math.max(0, Math.round(seconds));
  const hours = Math.floor(rounded / 3600);
  const minutes = Math.floor((rounded % 3600) / 60);
  const remainder = rounded % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`
    : `${minutes}:${String(remainder).padStart(2, "0")}`;
}

function formatImportBytes(bytes: number, formatNumber: FormatNumber): string {
  if (bytes < 1024 * 1024) return `${formatNumber(Math.max(1, Math.round(bytes / 1024)))} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${formatNumber(bytes / (1024 * 1024), { minimumFractionDigits: 1, maximumFractionDigits: 1 })} MB`;
  return `${formatNumber(bytes / (1024 * 1024 * 1024), { minimumFractionDigits: 2, maximumFractionDigits: 2 })} GB`;
}

function formatCapacity(seconds: number | null | undefined, t: Translate, formatNumber: FormatNumber): string {
  if (seconds == null || !Number.isFinite(seconds)) return t("Not verified");
  const hours = Math.max(0, seconds) / 3_600;
  if (hours >= 10) return `${formatNumber(Math.floor(hours))} h`;
  if (hours >= 1) return `${formatNumber(hours, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} h`;
  return `${formatNumber(Math.max(1, Math.floor(seconds / 60)))} min`;
}

const TERMINAL_IMPORT_STATES = new Set(["failed", "canceled", "completed"]);

function importStateTone(state: MeetingImportJob["state"]): string {
  if (state === "failed") return "text-amber-800 dark:text-amber-200";
  if (state === "canceled") return "text-muted-foreground";
  if (state === "finalizing" || state === "committing") return "text-blue-700 dark:text-blue-300";
  return "text-primary";
}

function MeetingImportInbox({
  items,
  loading,
  error,
  cancelingId,
  retryingMeetingId,
  onCancel,
  onRetry,
  onOpen,
  onRefresh,
}: {
  items: MeetingImportJob[];
  loading: boolean;
  error: boolean;
  cancelingId?: string;
  retryingMeetingId?: string;
  onCancel: (importId: string) => void;
  onRetry: (meetingId: string) => void;
  onOpen: (meetingId: string) => void;
  onRefresh: () => void;
}) {
  const { t, formatDate, formatNumber } = useI18n();
  return (
    <section className="my-1 border-y border-border/55 py-2" aria-labelledby="meeting-import-inbox-title">
      <div className="flex items-center justify-between gap-2 px-2 py-1">
        <div className="min-w-0">
          <p id="meeting-import-inbox-title" className="text-xs font-semibold">{t("Imports")}</p>
          <p className="text-[11px] text-muted-foreground">{t("Continues after you restart Scriber")}</p>
        </div>
        {!loading && !error && items.length > 0 && (
          <span className="rounded-full bg-muted px-2 py-0.5 font-mono text-[10px] tabular-nums text-muted-foreground">
            {formatNumber(items.length)}
          </span>
        )}
      </div>
      {loading ? (
        <div className="mt-1 grid gap-1 px-1 sm:grid-cols-2 lg:grid-cols-3 min-[1100px]:grid-cols-1" aria-label={t("Loading meeting imports")}>
          {[0, 1].map((item) => <div key={item} className="h-[76px] animate-pulse rounded-xl bg-muted/60" />)}
        </div>
      ) : error ? (
        <div className="mt-1 flex items-center justify-between gap-2 px-2 py-2 text-xs text-destructive" role="alert">
          <span>{t("Imports could not be loaded.")}</span>
          <Button type="button" size="sm" variant="ghost" className="h-7 px-2 text-xs" onClick={onRefresh}>
            <RefreshCw className="mr-1.5 h-3 w-3" />{t("Retry")}
          </Button>
        </div>
      ) : items.length === 0 ? (
        <p className="px-2 py-2 text-xs text-muted-foreground">{t("No pending or recently interrupted imports.")}</p>
      ) : (
        <div className="mt-1 grid gap-x-2 px-1 sm:grid-cols-2 lg:grid-cols-3 min-[1100px]:grid-cols-1">
          {items.map((job) => {
            const active = !TERMINAL_IMPORT_STATES.has(job.state);
            const canceling = cancelingId === job.id;
            const retrying = Boolean(job.meetingId && retryingMeetingId === job.meetingId);
            return (
              <article key={job.id} className="min-w-0 border-t border-border/45 px-2 py-2.5 first:border-t-0 min-[1100px]:first:border-t-0">
                <div className="flex min-w-0 items-start gap-2">
                  <span className={`mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-muted/65 ${importStateTone(job.state)}`} aria-hidden="true">
                    {active ? <Loader2 className="h-3.5 w-3.5 animate-spin motion-reduce:animate-none" /> : job.state === "failed" ? <AlertTriangle className="h-3.5 w-3.5" /> : <FileUp className="h-3.5 w-3.5" />}
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-xs font-semibold text-foreground" title={job.title}>{job.title}</p>
                    <div className="mt-0.5 flex min-w-0 items-center justify-between gap-2 text-[10px]">
                      <span className={`truncate font-medium ${importStateTone(job.state)}`}>{t(job.status)}</span>
                      <span className="shrink-0 text-muted-foreground">{formatMoment(job.updatedAt, formatDate)}</span>
                    </div>
                  </div>
                </div>
                {active && (
                  <div className="mt-2 h-1 overflow-hidden rounded-full bg-muted" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(job.progress * 100)} aria-label={t("{{title}} import progress", { title: job.title })}>
                    <div className="h-full origin-left rounded-full bg-primary transition-transform duration-200 motion-reduce:transition-none" style={{ transform: `scaleX(${Math.max(0.02, Math.min(1, job.progress))})` }} />
                  </div>
                )}
                {job.errorMessage && <p className="mt-1.5 line-clamp-2 text-[10px] leading-4 text-muted-foreground">{t(job.errorMessage)}</p>}
                {(job.meetingId || job.canCancel) && (
                  <div className="mt-2 flex flex-wrap items-center gap-1">
                    {job.meetingId && (
                      <Button type="button" size="sm" variant="ghost" className="h-7 px-2 text-[11px] active:scale-[0.97]" onClick={() => onOpen(job.meetingId!)}>
                        {t("Open meeting")}
                      </Button>
                    )}
                    {job.canRetry && job.meetingId && (
                      <Button type="button" size="sm" variant="outline" className="h-7 px-2 text-[11px] active:scale-[0.97]" disabled={retrying} onClick={() => onRetry(job.meetingId!)}>
                        {retrying && <Loader2 className="mr-1.5 h-3 w-3 animate-spin" />}{t("Retry")}
                      </Button>
                    )}
                    {job.canCancel && (
                      <Button type="button" size="sm" variant="ghost" className="h-7 px-2 text-[11px] text-muted-foreground hover:text-destructive active:scale-[0.97]" disabled={canceling} onClick={() => onCancel(job.id)}>
                        {canceling && <Loader2 className="mr-1.5 h-3 w-3 animate-spin" />}{t("Cancel")}
                      </Button>
                    )}
                  </div>
                )}
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}

function highlightTranscriptMatch(text: string, query: string) {
  const normalized = query.trim();
  if (!normalized) return text;
  const expression = new RegExp(`(${normalized.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "ig");
  return text.split(expression).map((part, index) => (
    part.toLocaleLowerCase() === normalized.toLocaleLowerCase()
      ? <mark key={`${part}-${index}`} className="rounded-sm bg-amber-200/80 px-0.5 text-inherit dark:bg-amber-400/30">{part}</mark>
      : part
  ));
}

type DisplayMeetingSegment = MeetingSegment & { label: string };

const VirtualMeetingTranscript = memo(function VirtualMeetingTranscript({
  segments,
  search,
  hasPlayableAudio,
  isLive,
  onPlay,
  canAssignSpeakers,
  onAssignSpeaker,
  canEdit,
  savingSegmentId,
  onSave,
  onUndo,
}: {
  segments: DisplayMeetingSegment[];
  search: string;
  hasPlayableAudio: boolean;
  isLive: boolean;
  onPlay: (startMs: number) => void;
  canAssignSpeakers: boolean;
  onAssignSpeaker: (speakerId: string) => void;
  canEdit: boolean;
  savingSegmentId: string;
  onSave: (segment: DisplayMeetingSegment, text: string) => void;
  onUndo: (segment: DisplayMeetingSegment) => void;
}) {
  const { t, formatNumber } = useI18n();
  const scrollRef = useRef<HTMLDivElement>(null);
  const [followLatest, setFollowLatest] = useState(true);
  const [editingId, setEditingId] = useState("");
  const [draft, setDraft] = useState("");
  const beginEdit = (segment: DisplayMeetingSegment) => {
    setEditingId(segment.id);
    setDraft(segment.text);
  };
  const cancelEdit = () => {
    setEditingId("");
    setDraft("");
  };
  const virtualizer = useVirtualizer({
    count: segments.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 88,
    overscan: 8,
    getItemKey: (index) => segments[index]?.id ?? index,
  });
  const scrollToLatest = useCallback(() => {
    if (segments.length === 0) return;
    virtualizer.scrollToIndex(segments.length - 1, { align: "end" });
    const viewport = scrollRef.current;
    if (viewport) viewport.scrollTop = viewport.scrollHeight;
  }, [segments.length, virtualizer]);
  useEffect(() => {
    if (!isLive) setFollowLatest(true);
  }, [isLive]);
  useEffect(() => {
    if (!isLive || !followLatest || search.trim() || editingId || segments.length === 0) return;
    let settledFrame = 0;
    const layoutFrame = window.requestAnimationFrame(() => {
      scrollToLatest();
      settledFrame = window.requestAnimationFrame(scrollToLatest);
    });
    return () => {
      window.cancelAnimationFrame(layoutFrame);
      if (settledFrame) window.cancelAnimationFrame(settledFrame);
    };
  }, [editingId, followLatest, isLive, scrollToLatest, search, segments]);
  return (
    <div className="relative">
      <div
        ref={scrollRef}
        className="max-h-[520px] overflow-y-auto pr-2"
        aria-label={t("Meeting transcript segments")}
        onScroll={(event) => {
          if (!isLive || search.trim()) return;
          const element = event.currentTarget;
          const atLatest = element.scrollHeight - element.clientHeight - element.scrollTop <= 40;
          setFollowLatest((current) => current === atLatest ? current : atLatest);
        }}
      >
        <div className="relative w-full" style={{ height: virtualizer.getTotalSize() }}>
        {virtualizer.getVirtualItems().map((virtualRow) => {
          const segment = segments[virtualRow.index];
          if (!segment) return null;
          return (
            <div
              key={segment.id}
              data-index={virtualRow.index}
              ref={virtualizer.measureElement}
              className="absolute left-0 top-0 w-full pb-1"
              style={{ transform: `translateY(${virtualRow.start}px)` }}
            >
              <div
                className="group grid w-full grid-cols-[112px_minmax(0,1fr)] gap-3 rounded-xl px-3 py-3 outline-none hover:bg-muted/50 focus-within:bg-muted/35 sm:grid-cols-[128px_minmax(0,1fr)]"
                tabIndex={canEdit ? 0 : -1}
                onKeyDown={(event) => {
                  if (canEdit && event.key.toLocaleLowerCase() === "e" && event.target === event.currentTarget) {
                    event.preventDefault();
                    beginEdit(segment);
                  }
                }}
              >
                <button
                  type="button"
                  onClick={() => onPlay(segment.startMs)}
                  disabled={!hasPlayableAudio}
                  className="self-start rounded-lg text-left text-[10px] tabular-nums outline-none enabled:active:scale-[0.98] focus-visible:ring-2 focus-visible:ring-primary disabled:cursor-default"
                  title={hasPlayableAudio ? t("Play {{start}} to {{end}}", { start: formatOffset(segment.startMs), end: formatOffset(segment.endMs) }) : t("Saved audio is unavailable")}
                  aria-label={t("Play transcript segment from {{start}} to {{end}}", { start: formatOffset(segment.startMs), end: formatOffset(segment.endMs) })}
                >
                  <span className="grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5 font-mono">
                    <span className="font-sans text-muted-foreground">{t("Start")}</span>
                    <span className="text-right font-medium text-primary">{formatOffset(segment.startMs)}</span>
                    <span className="font-sans text-muted-foreground">{t("End")}</span>
                    <span className="text-right font-medium text-primary">{formatOffset(segment.endMs)}</span>
                    <span className="font-sans text-muted-foreground">{t("Duration")}</span>
                    <span className="text-right text-muted-foreground">{formatNumber(segment.durationMs / 1000, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} s</span>
                  </span>
                  {segment.alignmentQuality === "estimated" && (
                    <span className="mt-1 block font-sans text-[9px] font-medium uppercase tracking-wide text-amber-700 dark:text-amber-300" title={t("Exact word timing was not available, so this time was estimated.")}>
                      {t("Estimated timing")}
                    </span>
                  )}
                </button>
                <div className="min-w-0">
                  <div className="flex min-w-0 items-center gap-2">
                    {canAssignSpeakers && segment.speakerId ? (
                      <button
                        type="button"
                        onClick={() => onAssignSpeaker(segment.speakerId!)}
                        data-testid={`meeting-transcript-speaker-${segment.id}`}
                        data-speaker-id={segment.speakerId}
                        className="group/speaker inline-flex min-w-0 items-center gap-1 rounded-md px-1 py-0.5 text-xs font-semibold text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-primary"
                        title={t("Assign a speaker name to {{speaker}}", { speaker: segment.label })}
                        aria-label={t("Assign a speaker name to {{speaker}}", { speaker: segment.label })}
                      >
                        <span className="truncate">{highlightTranscriptMatch(segment.label, search)}</span>
                        <Pencil className="h-3 w-3 shrink-0 opacity-50 transition-opacity group-hover/speaker:opacity-80 group-focus-visible/speaker:opacity-80 motion-reduce:transition-none" aria-hidden="true" />
                      </button>
                    ) : (
                      <span className="truncate text-xs font-semibold text-muted-foreground">{highlightTranscriptMatch(segment.label, search)}</span>
                    )}
                    {segment.editVersion > 0 && <Badge variant="outline" className="h-5 shrink-0 px-1.5 text-[9px]">{t("Edited")}</Badge>}
                  </div>
                  {editingId === segment.id ? (
                    <div className="mt-2 space-y-2">
                      <Textarea
                        value={draft}
                        onChange={(event) => setDraft(event.target.value)}
                        rows={3}
                        autoFocus
                        aria-label={t("Edit transcript for {{speaker}} at {{time}}", { speaker: segment.label, time: formatOffset(segment.startMs) })}
                        className="text-sm leading-6"
                        onKeyDown={(event) => {
                          if (event.key === "Escape") cancelEdit();
                          if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && draft.trim()) {
                            onSave(segment, draft.trim());
                            cancelEdit();
                          }
                        }}
                      />
                      <div className="flex flex-wrap items-center gap-2">
                        <Button type="button" size="sm" className="h-8 active:scale-[0.97]" disabled={!draft.trim() || savingSegmentId === segment.id} onClick={() => { onSave(segment, draft.trim()); cancelEdit(); }}>
                          {savingSegmentId === segment.id && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />}{t("Save correction")}
                        </Button>
                        <Button type="button" size="sm" variant="ghost" className="h-8 active:scale-[0.97]" onClick={cancelEdit}>
                          <X className="mr-1.5 h-3.5 w-3.5" />{t("Cancel")}
                        </Button>
                        <span className="text-[10px] text-muted-foreground">{t("Ctrl+Enter saves · Esc cancels")}</span>
                      </div>
                    </div>
                  ) : (
                    <>
                      <p className="mt-1 text-sm leading-6">{highlightTranscriptMatch(segment.text, search)}</p>
                      {canEdit && (
                        <div className="mt-2 flex items-center gap-1 opacity-100 sm:opacity-0 sm:group-hover:opacity-100 sm:group-focus-within:opacity-100">
                          <Button type="button" size="sm" variant="ghost" className="h-7 px-2 text-[11px] active:scale-[0.97]" onClick={() => beginEdit(segment)}>
                            <Pencil className="mr-1.5 h-3 w-3" />{t("Edit")}
                          </Button>
                          {segment.editVersion > 0 && (
                            <Button type="button" size="sm" variant="ghost" className="h-7 px-2 text-[11px] active:scale-[0.97]" disabled={savingSegmentId === segment.id} onClick={() => onUndo(segment)}>
                              <Undo2 className="mr-1.5 h-3 w-3" />{t("Undo latest")}
                            </Button>
                          )}
                        </div>
                      )}
                    </>
                  )}
                </div>
              </div>
            </div>
          );
        })}
        </div>
      </div>
      {isLive && !search.trim() && !followLatest && (
        <Button
          type="button"
          size="sm"
          className="absolute bottom-3 right-4 h-8 rounded-full px-3 text-[11px] shadow-lg active:scale-[0.97]"
          onClick={() => {
            setFollowLatest(true);
            scrollToLatest();
          }}
        >
          <ChevronDown className="mr-1.5 h-3.5 w-3.5" />{t("Latest text")}
        </Button>
      )}
    </div>
  );
});

function EvidenceList({ items, onCitation }: { items: unknown; onCitation?: (id: string) => void }) {
  const { t } = useI18n();
  if (!Array.isArray(items) || items.length === 0) {
    return <p className="py-12 text-center text-sm text-muted-foreground">{t("Nothing clear enough was found in the transcript.")}</p>;
  }
  return <div className="divide-y divide-border/60">{items.map((raw, index) => {
    const item = raw && typeof raw === "object" ? raw as Record<string, unknown> : { text: String(raw) };
    const citations = Array.isArray(item.segmentIds) ? item.segmentIds.map(String) : [];
    return <div key={`${String(item.text)}-${index}`} className="py-4">
      <p className="text-sm leading-6">{String(item.text || item.summary || item.title || "")}</p>
      {Boolean(item.owner || item.dueDate) && <p className="mt-1 text-xs text-muted-foreground">{item.owner ? t("Owner: {{owner}}", { owner: String(item.owner) }) : t("Unassigned")}{item.dueDate ? ` · ${t("Due {{date}}", { date: String(item.dueDate) })}` : ""}</p>}
      {citations.length > 0 && <div className="mt-2 flex flex-wrap gap-1.5">{citations.map((citation) => <button type="button" key={citation} onClick={() => onCitation?.(citation)}><Badge variant="outline" className="font-mono text-[10px] hover:border-primary">{citation.slice(0, 8)}</Badge></button>)}</div>}
    </div>;
  })}</div>;
}

function ActionItems({
  items,
  onChange,
  saving,
  onCitation,
}: {
  items: MeetingActionItem[];
  onChange: (item: MeetingActionItem, changes: Partial<Pick<MeetingActionItem, "text" | "owner" | "dueDate" | "status">>) => void;
  saving: boolean;
  onCitation?: (id: string) => void;
}) {
  const { t } = useI18n();
  if (items.length === 0) {
    return <p className="py-12 text-center text-sm text-muted-foreground">{t("No clear action items were found in the transcript.")}</p>;
  }
  return <div className="divide-y divide-border/60" aria-busy={saving}>{items.map((item) => (
    <div key={`${item.id}:${item.updatedAt}`} className="grid gap-3 py-4 sm:grid-cols-[32px_minmax(0,1fr)]">
      <button
        type="button"
        disabled={saving}
        onClick={() => onChange(item, { status: item.status === "done" ? "open" : "done" })}
        className={`mt-1 flex h-6 w-6 items-center justify-center rounded-full border active:scale-[0.97] ${item.status === "done" ? "border-emerald-500 bg-emerald-500 text-white" : "border-border hover:border-primary"}`}
        aria-label={item.status === "done" ? t("Reopen action item") : t("Complete action item")}
      >{item.status === "done" && <Check className="h-3.5 w-3.5" />}</button>
      <div className="min-w-0 space-y-2">
        <Input
          disabled={saving}
          defaultValue={item.text}
          className={`h-auto border-0 bg-transparent px-0 py-0 text-sm shadow-none focus-visible:ring-0 ${item.status === "done" ? "text-muted-foreground line-through" : ""}`}
          onBlur={(event) => event.target.value.trim() !== item.text && onChange(item, { text: event.target.value })}
        />
        <div className="grid gap-2 sm:grid-cols-2">
          <Input disabled={saving} defaultValue={item.owner ?? ""} placeholder={t("Owner")} className="h-8 text-xs" onBlur={(event) => event.target.value !== (item.owner ?? "") && onChange(item, { owner: event.target.value || null })} />
          <Input disabled={saving} type="date" defaultValue={item.dueDate ?? ""} className="h-8 text-xs" onBlur={(event) => event.target.value !== (item.dueDate ?? "") && onChange(item, { dueDate: event.target.value || null })} />
        </div>
        {item.segmentIds.length > 0 && <div className="flex flex-wrap gap-1.5">{item.segmentIds.map((citation) => <button type="button" key={citation} onClick={() => onCitation?.(citation)}><Badge variant="outline" className="font-mono text-[10px] hover:border-primary">{citation.slice(0, 8)}</Badge></button>)}</div>}
      </div>
    </div>
  ))}</div>;
}

async function fetchJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetchWithTimeout(apiUrl(path), { credentials: "include", signal }, 15_000);
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return response.json() as Promise<T>;
}

const MeetingElapsedTime = memo(function MeetingElapsedTime({
  startedAt,
  audioGaps,
  paused,
  pausedAtTimelineMs,
  pausedAtUtc,
  recordingTimelineOffsetMs,
  recordingTimelineStartedAtUtc,
  finalProviderMaxDurationSeconds,
}: {
  startedAt: string | null;
  audioGaps: MeetingDetail["audioGaps"];
  paused: boolean;
  pausedAtTimelineMs?: unknown;
  pausedAtUtc?: unknown;
  recordingTimelineOffsetMs?: unknown;
  recordingTimelineStartedAtUtc?: unknown;
  finalProviderMaxDurationSeconds?: number | null;
}) {
  const { t } = useI18n();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (paused) return;
    const handle = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(handle);
  }, [paused]);
  const elapsedMs = calculateMeetingElapsedMs(
    startedAt,
    now,
    audioGaps,
    paused ? pausedAtTimelineMs : undefined,
    paused ? pausedAtUtc : undefined,
    paused ? undefined : recordingTimelineOffsetMs,
    paused ? undefined : recordingTimelineStartedAtUtc,
  );
  const providerRemainingMs = finalProviderMaxDurationSeconds == null
    ? null
    : finalProviderMaxDurationSeconds * 1_000 - elapsedMs;
  const showProviderLimit = providerRemainingMs != null && providerRemainingMs <= 30 * 60 * 1_000;
  return (
    <div className="order-first text-left sm:order-none sm:text-center" aria-label={t("Meeting elapsed time {{time}}", { time: formatOffset(elapsedMs) })}>
      <p className="font-mono text-2xl font-semibold tabular-nums tracking-tight">{formatOffset(elapsedMs)}</p>
      <p className="mt-0.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-primary">
        {paused ? t("Recording paused") : t("Recording")}
      </p>
      {showProviderLimit && <p className="mt-1 text-[10px] font-semibold text-amber-700 dark:text-amber-300" role="status">
        {providerRemainingMs > 0
          ? t("Final transcript time remaining: {{time}}", { time: formatOffset(providerRemainingMs) })
          : t("This transcription option has reached its time limit")}
      </p>}
    </div>
  );
});

const MeetingCheckpointStatus = memo(function MeetingCheckpointStatus({
  checkpoint,
  expectedTrackCount,
  paused,
}: {
  checkpoint: MeetingDetail["transcriptCheckpoints"][number] | undefined;
  expectedTrackCount: number;
  paused: boolean;
}) {
  const { t, formatNumber } = useI18n();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!checkpoint || paused) return;
    setNow(Date.now());
    const handle = window.setInterval(() => setNow(Date.now()), 5_000);
    return () => window.clearInterval(handle);
  }, [checkpoint, paused]);
  if (!checkpoint) {
    return <span>{t("First safety save at 0:30")}</span>;
  }
  const freshness = meetingCheckpointFreshness(checkpoint.updatedAt, now, paused);
  const ageLabel = /^([0-9]+) s ago$/.exec(freshness.ageLabel);
  const localizedAge = ageLabel
    ? t("{{seconds}} s ago", { seconds: formatNumber(Number(ageLabel[1])) })
    : t(freshness.ageLabel);
  return (
    <span className={freshness.stale ? "text-amber-700 dark:text-amber-300" : undefined}>
      {t("Protected through {{time}} · {{age}} · {{current}}/{{expected}} audio sources", {
        time: formatOffset(checkpoint.cutoffMs),
        age: localizedAge,
        current: formatNumber(checkpoint.sources.length),
        expected: formatNumber(expectedTrackCount),
      })}
    </span>
  );
});

const MeetingLevelMeter = memo(function MeetingLevelMeter({
  source,
  paused,
  levels,
}: {
  source: "microphone" | "system";
  paused: boolean;
  levels: { current: Record<"microphone" | "system", number> };
}) {
  const { t } = useI18n();
  const barRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let frame = 0;
    const paint = () => {
      const value = paused ? 0 : levels.current[source];
      if (barRef.current) {
        barRef.current.style.transform = `scaleX(${Math.min(1, Math.max(0.02, value * 2.4))})`;
      }
      frame = window.requestAnimationFrame(paint);
    };
    frame = window.requestAnimationFrame(paint);
    return () => window.cancelAnimationFrame(frame);
  }, [levels, paused, source]);
  return (
    <div className="flex min-w-0 items-center gap-3 rounded-lg border border-border/55 bg-background/35 px-3 py-2">
      {source === "microphone"
        ? <Mic2 className="h-4 w-4 shrink-0 text-primary" />
        : <Headphones className="h-4 w-4 shrink-0 text-primary" />}
      <div className="min-w-0 flex-1">
        <div className="mb-1 flex items-center justify-between text-[11px]">
          <span className="font-medium">{source === "microphone" ? t("Microphone") : t("System audio")}</span>
          <span className="text-emerald-600 dark:text-emerald-300">{paused ? t("Paused") : t("Healthy")}</span>
        </div>
        <div className="h-1 overflow-hidden rounded-full bg-muted" aria-hidden="true">
          <div ref={barRef} className="h-full origin-left rounded-full bg-primary motion-reduce:transition-none" style={{ transform: "scaleX(0.02)" }} />
        </div>
      </div>
    </div>
  );
});

const MeetingWorkspaceTabs = memo(function MeetingWorkspaceTabs({
  value,
  onChange,
}: {
  value: MeetingWorkspaceView;
  onChange: (value: MeetingWorkspaceView) => void;
}) {
  const { t } = useI18n();
  const scrollerRef = useRef<HTMLElement>(null);
  const [overflow, setOverflow] = useState({ left: false, right: false });
  const updateOverflow = useCallback(() => {
    const node = scrollerRef.current;
    if (!node) return;
    setOverflow({
      left: node.scrollLeft > 2,
      right: node.scrollLeft + node.clientWidth < node.scrollWidth - 2,
    });
  }, []);
  useEffect(() => {
    const node = scrollerRef.current;
    if (!node) return;
    updateOverflow();
    const observer = new ResizeObserver(updateOverflow);
    observer.observe(node);
    return () => observer.disconnect();
  }, [updateOverflow]);
  const scroll = (direction: -1 | 1) => {
    scrollerRef.current?.scrollBy({ left: direction * 220, behavior: "smooth" });
  };
  return (
    <div className="relative border-b border-border/60">
      <nav
        ref={scrollerRef}
        className="flex gap-1 overflow-x-auto px-11 sm:px-12"
        aria-label={t("Meeting workspace views")}
        onScroll={updateOverflow}
      >
        {MEETING_WORKSPACE_VIEWS.map(([view, label]) => (
          <button
            key={view}
            type="button"
            onClick={() => onChange(view)}
            className={`whitespace-nowrap border-b-2 px-3 py-2 text-sm font-medium active:scale-[0.97] ${value === view ? "border-primary text-foreground" : "border-transparent text-muted-foreground hover:text-foreground"}`}
            aria-current={value === view ? "page" : undefined}
          >
            {t(label)}
          </button>
        ))}
      </nav>
      <button
        type="button"
        onClick={() => scroll(-1)}
        disabled={!overflow.left}
        className="absolute inset-y-0 left-0 grid w-10 place-items-center border-r border-border/50 bg-card/95 text-muted-foreground disabled:opacity-30"
        aria-label={t("Previous meeting views")}
      >
        <ChevronLeft className="h-4 w-4" />
      </button>
      <button
        type="button"
        onClick={() => scroll(1)}
        disabled={!overflow.right}
        className="absolute inset-y-0 right-0 grid w-10 place-items-center border-l border-border/50 bg-card/95 text-muted-foreground disabled:opacity-30"
        aria-label={t("More meeting views")}
      >
        <ChevronRight className="h-4 w-4" />
      </button>
    </div>
  );
});

export default function Meetings({ params }: { params?: { id?: string } }) {
  const { t, formatDate, formatNumber, localeTag } = useI18n();
  const selectedId = params?.id || "";
  const newMeetingSetupEnabled = isNewMeetingSetupEnabled(selectedId);
  const [, setLocation] = useLocation();
  const queryClient = useQueryClient();
  const { isConnected: meetingWsConnected } = useWebSocketContext();
  const { toast } = useToast();
  const [title, setTitle] = useState("");
  const [selectedCalendarEventId, setSelectedCalendarEventId] = useState("");
  const [calendarSelectionNeedsReview, setCalendarSelectionNeedsReview] = useState(false);
  const calendarSelectionInitializedRef = useRef(false);
  const selectedCalendarSubjectRef = useRef("");
  const [note, setNote] = useState("");
  const [noteHydratedFor, setNoteHydratedFor] = useState("");
  const lastSavedNote = useRef("");
  const noteDraftRef = useRef({ meetingId: "", body: "", savedBody: "" });
  const [workspaceView, setWorkspaceView] = useState<MeetingWorkspaceView>("transcript");
  const [chatQuestion, setChatQuestion] = useState("");
  const [chatAnswer, setChatAnswer] = useState<{ content: string; citations: string[] } | null>(null);
  const audioLevelsRef = useRef({ microphone: 0, system: 0 });
  const audioRef = useRef<HTMLAudioElement>(null);
  const meetingImportRef = useRef<HTMLInputElement>(null);
  const meetingImportIdRef = useRef("");
  const meetingImportExplicitCancelRef = useRef(false);
  const pendingPlaybackRef = useRef<MeetingPlaybackRequest | null>(null);
  const speakerSnippetEndMsRef = useRef<number | null>(null);
  const speakerAssignmentRequestIdRef = useRef(0);
  const savedVoicePreviewRef = useRef<HTMLAudioElement | null>(null);
  const savedVoicePreviewProfileIdRef = useRef("");
  const [playbackError, setPlaybackError] = useState("");
  const [speakerAssignmentRequest, setSpeakerAssignmentRequest] = useState<MeetingSpeakerAssignmentFocusRequest | null>(null);
  const [meetingPendingDelete, setMeetingPendingDelete] = useState<MeetingSummary | null>(null);
  const [transcriptSearch, setTranscriptSearch] = useState("");
  const [emailDialogOpen, setEmailDialogOpen] = useState(false);
  const [reprocessDialogOpen, setReprocessDialogOpen] = useState(false);
  const [reprocessMode, setReprocessMode] = useState<MeetingReprocessMode>("speaker_identity");
  const [lastExport, setLastExport] = useState<Extract<MeetingExportResult, { status: "saved" }> | null>(null);
  const [meetingImportCandidate, setMeetingImportCandidate] = useState<{
    file: File;
    title: string;
    durationSeconds: number | null;
  } | null>(null);
  const [meetingImportId, setMeetingImportId] = useState("");
  const [meetingImportProgress, setMeetingImportProgress] = useState<MeetingImportProgressView>({
    importId: "",
    phase: "created",
    stage: "Ready",
    percentage: 0,
  });
  const [emailAttachment, setEmailAttachment] = useState<MeetingEmailDraftAttachment>("pdf");
  const [retryFinalProvider, setRetryFinalProvider] = useState("");
  const [microphoneEndpointHash, setMicrophoneEndpointHash] = useState("");
  const [renderEndpointHash, setRenderEndpointHash] = useState("");
  const [webhookUrl, setWebhookUrl] = useState("");
  const [webhookSecret, setWebhookSecret] = useState("");
  const [webhookConfirmed, setWebhookConfirmed] = useState(false);
  const [webhookPreview, setWebhookPreview] = useState<{
    target: string;
    previewHash: string;
    byteSize: number;
    payload: { event?: string; meeting?: { title?: string }; segments?: unknown[]; notes?: unknown[] };
  } | null>(null);
  const [meetingProgress, setMeetingProgress] = useState<MeetingProcessingProgress | null>(null);
  const [liveStatuses, setLiveStatuses] = useState<Record<"microphone" | "system", { status: "reconnecting" | "recovered" | "degraded"; reconnectCount: number } | null>>({ microphone: null, system: null });
  const meetingWsHasConnectedRef = useRef(false);
  const meetingWsWasConnectedRef = useRef(false);

  useEffect(() => setLastExport(null), [selectedId]);

  const meetingsQuery = useInfiniteQuery<MeetingsResponse, Error>({
    queryKey: MEETING_HISTORY_QUERY_KEY,
    queryFn: ({ pageParam, signal }) => {
      const offset = typeof pageParam === "number" ? pageParam : 0;
      return fetchJson(`/api/meetings?limit=100&offset=${offset}`, signal);
    },
    initialPageParam: 0,
    getNextPageParam: (lastPage) => {
      const nextOffset = lastPage.offset + lastPage.items.length;
      return nextOffset < lastPage.total && nextOffset > lastPage.offset
        ? nextOffset
        : undefined;
    },
    staleTime: 10_000,
  });
  const meetingImportsQuery = useQuery<MeetingImportsResponse>({
    queryKey: MEETING_IMPORTS_QUERY_KEY,
    queryFn: ({ signal }) => fetchJson("/api/meeting-imports?limit=24", signal),
    staleTime: 5_000,
    refetchInterval: meetingImportId && !meetingWsConnected ? 2_000 : false,
  });
  const capabilitiesQuery = useQuery<MeetingCapabilities>({
    queryKey: ["/api/meetings/capabilities"],
    queryFn: ({ signal }) => fetchJson("/api/meetings/capabilities", signal),
    staleTime: 5_000,
  });
  const profilesQuery = useQuery<MeetingProfilesResponse>({
    queryKey: ["/api/meeting-profiles"],
    queryFn: ({ signal }) => fetchJson("/api/meeting-profiles", signal),
    staleTime: 30_000,
  });
  const audioDevicesQuery = useQuery<MeetingAudioDevicesResponse>({
    queryKey: ["/api/meetings/audio-devices"],
    queryFn: ({ signal }) => fetchJson("/api/meetings/audio-devices", signal),
    staleTime: 10_000,
    enabled: newMeetingSetupEnabled,
    refetchInterval: selectedId ? false : 15_000,
  });
  useEffect(() => {
    const inventory = audioDevicesQuery.data;
    if (!inventory?.available) return;
    setMicrophoneEndpointHash((current) => (
      current && !inventory.capture.some((endpoint) => endpoint.endpointIdHash === current) ? "" : current
    ));
    setRenderEndpointHash((current) => (
      current && !inventory.render.some((endpoint) => endpoint.endpointIdHash === current) ? "" : current
    ));
  }, [audioDevicesQuery.data]);
  const detectionQuery = useQuery<MeetingDetectionResponse>({
    queryKey: ["/api/meetings/detection"],
    queryFn: ({ signal }) => fetchJson("/api/meetings/detection", signal),
    staleTime: 2_000,
    enabled: newMeetingSetupEnabled,
    refetchInterval: selectedId ? false : 5_000,
  });
  const outlookQuery = useQuery<OutlookCalendarStatus>({
    queryKey: ["/api/calendar/outlook/status"],
    queryFn: ({ signal }) => fetchJson("/api/calendar/outlook/status", signal),
    staleTime: 15_000,
    enabled: newMeetingSetupEnabled,
    refetchInterval: (query) => (
      query.state.data?.authorizationPending
        || (
          query.state.data?.connected
          && !query.state.data.lastSyncAt
          && !query.state.data.lastError
        )
        ? 2_000
        : 30_000
    ),
  });
  const outlookCalendarNow = new Date();
  const outlookCalendarDate = localCalendarDate(outlookCalendarNow);
  const outlookTimeZone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const outlookCalendarWindow = localCalendarDayWindow(outlookCalendarNow);
  const outlookEventsQuery = useQuery<OutlookCalendarEventsResponse>({
    queryKey: ["/api/calendar/outlook/events", outlookCalendarDate, outlookTimeZone, outlookQuery.data?.lastSyncAt ?? ""],
    queryFn: ({ signal }) => {
      const query = new URLSearchParams({
        date: outlookCalendarDate,
        timeZone: outlookTimeZone,
        start: outlookCalendarWindow.start,
        end: outlookCalendarWindow.end,
      });
      return fetchJson(`/api/calendar/outlook/events?${query.toString()}`, signal);
    },
    enabled: Boolean(newMeetingSetupEnabled && outlookQuery.data?.connected && !outlookQuery.data.authorizationPending && outlookQuery.data.lastSyncAt),
    staleTime: 15_000,
  });
  const outlookSyncMutation = useMutation({
    mutationFn: async () => {
      const response = await apiRequest(
        "POST",
        "/api/calendar/outlook/sync",
        undefined,
        { timeoutMs: OUTLOOK_SYNC_REQUEST_TIMEOUT_MS },
      );
      return response.json() as Promise<OutlookCalendarSyncResponse>;
    },
    onSuccess: (status) => {
      // The sync response already contains the credential-backed status. Reuse
      // it instead of issuing another named-pipe status request. `lastSyncAt`
      // is part of the events query key, so this causes exactly one fresh daily
      // events request rather than refetching both the old and new keys.
      queryClient.setQueryData(["/api/calendar/outlook/status"], status);
      toast({ title: t("Outlook calendar refreshed") });
    },
    onError: (error) => {
      // A rejected refresh token changes the status to `reauthRequired` in the
      // backend. Refresh this small status query immediately so the Meeting UI
      // offers reconnection instead of continuing to look connected.
      void queryClient.invalidateQueries({
        queryKey: ["/api/calendar/outlook/status"],
        exact: true,
      });
      toast({ variant: "destructive", title: t("Outlook calendar could not refresh"), description: t(error.message) });
    },
  });
  const speakerModelQuery = useQuery<SpeakerModelStatus>({
    queryKey: ["/api/meetings/speaker-model"],
    queryFn: ({ signal }) => fetchJson("/api/meetings/speaker-model", signal),
    staleTime: 30_000,
    enabled: newMeetingSetupEnabled,
  });
  useEffect(() => {
    if (!newMeetingSetupEnabled) return;
    if (
      outlookQuery.data
      && outlookQuery.data.credentialStatusAvailable !== false
      && (!outlookQuery.data.connected || outlookQuery.data.authorizationPending)
    ) {
      if (outlookQuery.data.reauthRequired) {
        // A revoked credential makes the old calendar link unsafe for a new
        // Meeting. Keep the explicit review state even after clearing the id so
        // the user must reconnect, reselect, or consciously continue unlinked.
        if (selectedCalendarEventId) setCalendarSelectionNeedsReview(true);
      } else {
        setCalendarSelectionNeedsReview(false);
      }
      setSelectedCalendarEventId("");
      calendarSelectionInitializedRef.current = false;
      selectedCalendarSubjectRef.current = "";
      return;
    }
    const events = outlookEventsQuery.data?.items;
    if (!events) return;
    if (!calendarSelectionInitializedRef.current) {
      calendarSelectionInitializedRef.current = true;
      const suggested = events.find((event) => event.id === outlookQuery.data?.nextEvent?.id)
        ?? (events.length === 1 ? events[0] : null);
      if (suggested) {
        setSelectedCalendarEventId(suggested.id);
        setCalendarSelectionNeedsReview(false);
        setTitle((current) => current.trim() ? current : suggested.subject);
        selectedCalendarSubjectRef.current = suggested.subject;
      }
      return;
    }
    const selected = events.find((event) => event.id === selectedCalendarEventId);
    if (selectedCalendarEventId && !selected) {
      setSelectedCalendarEventId("");
      setCalendarSelectionNeedsReview(true);
      selectedCalendarSubjectRef.current = "";
      return;
    }
    if (selected) {
      const previousSubject = selectedCalendarSubjectRef.current;
      if (selected.subject !== previousSubject) {
        setTitle((currentTitle) => (
          !currentTitle.trim() || currentTitle === previousSubject
            ? selected.subject
            : currentTitle
        ));
        selectedCalendarSubjectRef.current = selected.subject;
      }
    }
  }, [newMeetingSetupEnabled, outlookEventsQuery.data?.items, outlookQuery.data, outlookQuery.data?.nextEvent?.id, selectedCalendarEventId]);
  const detailQuery = useQuery<MeetingDetail>({
    queryKey: ["/api/meetings", selectedId],
    queryFn: ({ signal }) => fetchJson(`/api/meetings/${selectedId}`, signal),
    enabled: Boolean(selectedId),
    staleTime: 30_000,
    refetchInterval: (query) => meetingDetailRefetchInterval(
      query.state.data?.state,
      meetingWsConnected,
    ),
  });
  const deliveriesQuery = useQuery<{ items: Array<{ id: string; target: string; status: string; attemptCount: number }> }>({
    queryKey: ["/api/meetings", selectedId, "deliveries"],
    queryFn: ({ signal }) => fetchJson(`/api/meetings/${selectedId}/deliveries`, signal),
    enabled: Boolean(selectedId),
    staleTime: 5_000,
  });
  const detail = detailQuery.data;
  useEffect(() => {
    if (!detail || !["stopping", "finalizing", "analyzing"].includes(detail.state)) {
      setMeetingProgress(null);
      return;
    }
    const persistedProgress = detail.processingProgress;
    if (!persistedProgress) return;
    setMeetingProgress((current) => mergeMeetingProcessingProgress(
      current,
      persistedProgress,
    ));
  }, [detail?.id, detail?.processingProgress, detail?.state]);
  const speakerProfilesQuery = useQuery<SpeakerProfilesResponse>({
    queryKey: ["/api/meetings/speaker-profiles"],
    queryFn: ({ signal }) => fetchJson("/api/meetings/speaker-profiles", signal),
    enabled: Boolean(detail?.speakers.some((speaker) => speaker.profileId)),
    staleTime: 30_000,
  });
  const speakerProfilesById = useMemo(() => new Map(
    (speakerProfilesQuery.data?.items ?? []).map((profile) => [profile.id, profile]),
  ), [speakerProfilesQuery.data?.items]);
  const emailPreviewQuery = useQuery<{
    recipients: Array<{ name: string; address: string }>;
    subject: string;
    body: string;
  }>({
    queryKey: ["/api/meetings", selectedId, "email-preview"],
    queryFn: ({ signal }) => fetchJson(`/api/meetings/${selectedId}/email-preview`, signal),
    enabled: Boolean(selectedId && emailDialogOpen),
    staleTime: 10_000,
  });

  useEffect(() => {
    setEmailDialogOpen(false);
    setReprocessDialogOpen(false);
    setReprocessMode("speaker_identity");
    setEmailAttachment("pdf");
    setWebhookUrl("");
    setWebhookPreview(null);
    setWebhookConfirmed(false);
    setWebhookSecret("");
    audioLevelsRef.current = { microphone: 0, system: 0 };
    setMeetingProgress(null);
    setLiveStatuses({ microphone: null, system: null });
    pendingPlaybackRef.current = null;
    speakerSnippetEndMsRef.current = null;
    setSpeakerAssignmentRequest(null);
    savedVoicePreviewRef.current?.pause();
    savedVoicePreviewProfileIdRef.current = "";
    audioRef.current?.pause();
    setChatQuestion("");
    setChatAnswer(null);
    setTranscriptSearch("");
    setRetryFinalProvider("");
    setNote("");
    setNoteHydratedFor("");
  }, [selectedId]);

  useEffect(() => () => {
    const preview = savedVoicePreviewRef.current;
    if (!preview) return;
    preview.pause();
    preview.removeAttribute("src");
    savedVoicePreviewRef.current = null;
  }, []);

  useEffect(() => {
    if (!detail || noteHydratedFor === detail.id) return;
    const workspaceNote = detail.notes.find((item) => item.id === "workspace")?.body ?? "";
    setNote(workspaceNote);
    lastSavedNote.current = workspaceNote;
    noteDraftRef.current = { meetingId: detail.id, body: workspaceNote, savedBody: workspaceNote };
    setNoteHydratedFor(detail.id);
  }, [detail, noteHydratedFor]);

  useEffect(() => {
    if (detail?.id && detail.state === "finalization_failed") {
      setRetryFinalProvider(detail.finalProvider);
    }
  }, [detail?.finalProvider, detail?.id, detail?.state]);

  const refreshMeetingData = useCallback((meetingId?: string) => {
    void refreshMeetingCollections(queryClient);
    void refreshMeetingCapabilities(queryClient);
    if (meetingId) void refreshMeetingDetail(queryClient, meetingId);
  }, [queryClient]);

  const invalidateMeetingImports = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: MEETING_IMPORTS_QUERY_KEY, exact: true });
  }, [queryClient]);

  const handleWsMessage = useCallback((message: ScriberWebSocketMessage) => {
    if (message.type === "microphones_updated") {
      void queryClient.invalidateQueries({ queryKey: ["/api/meetings/audio-devices"], exact: true });
    }
    if (message.type === "meeting_state") {
      applyMeetingSummaryEvent(queryClient, message.meeting);
      if (TERMINAL_MEETING_STATES.has(message.meeting.state)) {
        void queryClient.invalidateQueries({ queryKey: ["/api/meetings", message.meeting.id], exact: true });
        void queryClient.invalidateQueries({ queryKey: ["/api/meetings/speaker-profiles"] });
        void queryClient.invalidateQueries({ queryKey: ["/api/meetings", message.meeting.id, "speaker-assignments"], exact: true });
        void queryClient.invalidateQueries({ queryKey: ["/api/meetings", message.meeting.id, "email-preview"], exact: true });
      }
      if (message.meeting.id === selectedId && !["stopping", "finalizing", "analyzing"].includes(message.meeting.state)) {
        setMeetingProgress(null);
      }
      if (message.meeting.id === selectedId && TERMINAL_MEETING_STATES.has(message.meeting.state)) {
        audioLevelsRef.current = { microphone: 0, system: 0 };
      }
    }
    if ((message.type === "meeting_finalize_progress" || message.type === "meeting_analysis_progress") && message.meetingId === selectedId) {
      const incoming: MeetingProcessingProgress = {
        phase: message.type === "meeting_analysis_progress" ? "analysis" : "finalize",
        progress: Math.max(0, Math.min(1, message.progress)),
        status: message.status,
        updatedAt: new Date().toISOString(),
      };
      applyMeetingProgressEvent(queryClient, message.meetingId, incoming);
      setMeetingProgress((current) => mergeMeetingProcessingProgress(current, incoming));
      if (message.type === "meeting_analysis_progress" && message.progress >= 1) {
        if (message.status === "Speaker matches refreshed") {
          toast({ title: t("Speaker matches refreshed"), description: t("The latest saved voices are now reflected in this meeting.") });
        } else if (message.status === "Speaker matches could not be refreshed") {
          toast({
            variant: "destructive",
            title: t("Speaker matches were not refreshed"),
            description: t("The meeting and its existing speaker names were left unchanged. You can try again."),
          });
        }
      }
    }
    if (message.type === "meeting_segment") {
      applyMeetingSegmentEvent(queryClient, message.meetingId, message.segment);
    }
    if (message.type === "meeting_checkpoint") {
      applyMeetingCheckpointEvent(queryClient, message.meetingId, message.checkpoint);
    }
    if (message.type === "meeting_transcript_edited") {
      applyMeetingTranscriptEditedEvent(
        queryClient,
        message.meetingId,
        message.segment,
        message.transcriptEditVersion,
      );
    }
    if (message.type === "meeting_audio_level" && message.meetingId === selectedId) {
      if (message.source === "microphone" || message.source === "system") {
        audioLevelsRef.current[message.source] = message.rms;
      }
    }
    if (message.type === "meeting_live_status" && message.meetingId === selectedId && (message.source === "microphone" || message.source === "system")) {
      setLiveStatuses((current) => ({
        ...current,
        [message.source]: { status: message.status, reconnectCount: message.reconnectCount },
      }));
    }
    if (message.type === "meeting_note") {
      applyMeetingNoteEvent(queryClient, message.meetingId, message.note);
    }
    if (message.type === "meeting_import_progress") {
      applyMeetingImportProgressEvent(queryClient, message);
      if (message.importId === meetingImportId) {
        setMeetingImportProgress((current) => mergeMeetingImportProgress(current, {
          importId: message.importId,
          phase: message.phase,
          stage: message.status,
          percentage: Math.round(Math.max(0, Math.min(1, message.progress)) * 100),
        }));
        if (message.meetingId) {
          meetingImportIdRef.current = "";
          setMeetingImportId("");
          setMeetingImportCandidate(null);
          refreshMeetingData(message.meetingId);
          setLocation(`/meetings/${message.meetingId}`);
          toast({ title: t("Meeting created"), description: t("Scriber is preparing the transcript and speaker names.") });
        } else if (message.phase === "failed" || message.phase === "canceled") {
          meetingImportIdRef.current = "";
          setMeetingImportId("");
          if (message.phase === "canceled") {
            meetingImportExplicitCancelRef.current = false;
            setMeetingImportCandidate(null);
          }
        }
      }
    }
  }, [meetingImportId, queryClient, refreshMeetingData, selectedId, setLocation, t, toast]);
  useSharedWebSocket(handleWsMessage);

  useEffect(() => {
    if (isMeetingWebSocketReconnect(
      meetingWsHasConnectedRef.current,
      meetingWsWasConnectedRef.current,
      meetingWsConnected,
    )) {
      // ActiveMeetingPill owns the one exact active-Meeting refresh on a real
      // reconnect. Refresh only this page's non-overlapping cache shapes here.
      void queryClient.invalidateQueries({ queryKey: MEETING_HISTORY_QUERY_KEY, exact: true });
      void refreshMeetingCapabilities(queryClient);
      if (selectedId) void refreshMeetingDetail(queryClient, selectedId);
      invalidateMeetingImports();
    }
    if (meetingWsConnected) meetingWsHasConnectedRef.current = true;
    meetingWsWasConnectedRef.current = meetingWsConnected;
  }, [invalidateMeetingImports, meetingWsConnected, queryClient, selectedId]);

  useEffect(() => {
    const job = meetingImportsQuery.data?.items.find((item) => item.id === meetingImportId);
    if (!job) return;
    setMeetingImportProgress((current) => mergeMeetingImportProgress(current, {
      importId: job.id,
      phase: job.state,
      stage: job.status || job.state,
      percentage: Math.round(Math.max(0, Math.min(1, job.progress)) * 100),
    }));
    if (job.meetingId) {
      meetingImportIdRef.current = "";
      setMeetingImportId("");
      setMeetingImportCandidate(null);
      refreshMeetingData(job.meetingId);
      setLocation(`/meetings/${job.meetingId}`);
      toast({ title: t("Meeting created"), description: t("Scriber is preparing the transcript and speaker names.") });
    } else if (["failed", "canceled"].includes(job.state)) {
      meetingImportIdRef.current = "";
      meetingImportExplicitCancelRef.current = false;
      setMeetingImportId("");
      if (job.state === "canceled") setMeetingImportCandidate(null);
    }
  }, [meetingImportId, meetingImportsQuery.data?.items, refreshMeetingData, setLocation, t, toast]);

  const startMutation = useMutation({
    mutationFn: async () => {
      const profile = profilesQuery.data?.profiles.find((item) => item.id === profilesQuery.data?.defaultProfileId)
        ?? profilesQuery.data?.profiles[0];
      const response = await apiRequest("POST", "/api/meetings", {
        title,
        language: profile?.language ?? "auto",
        transcriptionMode: profile?.transcriptionMode ?? "live_final",
        liveProvider: profile?.liveProvider ?? "soniox",
        finalProvider: profile?.finalProvider ?? "soniox_async",
        analysisModel: profile?.analysisModel ?? "",
        aecEnabled: profile?.aecEnabled ?? true,
        voiceLibraryEnabled: Boolean(speakerModelQuery.data?.optedIn && speakerModelQuery.data?.installed),
        audioRetentionDays: profile?.audioRetentionDays ?? 0,
        smartTurnEnabled: profile?.smartTurnEnabled ?? true,
        autoAnalyze: profile?.autoAnalyze ?? true,
        microphoneNativeEndpointIdHash: microphoneEndpointHash,
        renderNativeEndpointIdHash: renderEndpointHash,
        calendarEventId: selectedCalendarEventId || null,
      });
      return response.json() as Promise<MeetingSummary>;
    },
    onSuccess: (meeting) => {
      setTitle("");
      setSelectedCalendarEventId("");
      setCalendarSelectionNeedsReview(false);
      calendarSelectionInitializedRef.current = false;
      selectedCalendarSubjectRef.current = "";
      applyMeetingSummaryEvent(queryClient, meeting);
      setLocation(`/meetings/${meeting.id}`);
    },
    onError: (error) => toast({ variant: "destructive", title: t("Meeting could not start"), description: t(error.message) }),
  });
  const deviceTestMutation = useMutation({
    mutationFn: async () => {
      const profile = profilesQuery.data?.profiles.find((item) => item.id === profilesQuery.data?.defaultProfileId)
        ?? profilesQuery.data?.profiles[0];
      const response = await apiRequest("POST", "/api/meetings/device-test", {
        microphoneNativeEndpointIdHash: microphoneEndpointHash,
        renderNativeEndpointIdHash: renderEndpointHash,
        aecEnabled: profile?.aecEnabled ?? true,
        durationMs: 3_000,
        playTestTone: true,
      });
      return response.json() as Promise<MeetingDeviceTestResponse>;
    },
    onError: (error) => toast({ variant: "destructive", title: t("Audio routes could not be tested"), description: t(error.message) }),
  });
  const detectionDismissMutation = useMutation({
    mutationFn: async (detectionId: string) => {
      const response = await apiRequest("POST", "/api/meetings/detection/dismiss", { detectionId });
      return response.json();
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["/api/meetings/detection"] }),
    onError: (error) => toast({ variant: "destructive", title: t("Suggestion could not be dismissed"), description: t(error.message) }),
  });
  const webhookPreviewMutation = useMutation({
    mutationFn: async ({ id, url }: { id: string; url: string }) => {
      const response = await apiRequest("POST", `/api/meetings/${id}/deliveries/preview`, { url });
      return response.json() as Promise<NonNullable<typeof webhookPreview>>;
    },
    onSuccess: (preview, variables) => {
      if (variables.id !== selectedId) return;
      setWebhookPreview(preview);
      setWebhookConfirmed(false);
    },
    onError: (error, variables) => {
      if (variables.id === selectedId) {
        toast({ variant: "destructive", title: t("Webhook preview failed"), description: t(error.message) });
      }
    },
  });
  const webhookDeliveryMutation = useMutation({
    mutationFn: async ({ id, url, secret, previewHash, confirmed }: { id: string; url: string; secret: string; previewHash: string; confirmed: boolean }) => {
      const response = await apiRequest("POST", `/api/meetings/${id}/deliveries`, {
        url,
        secret,
        previewHash,
        confirmed,
      });
      return response.json();
    },
    onSuccess: (_payload, variables) => {
      if (variables.id === selectedId) {
        setWebhookSecret("");
        setWebhookConfirmed(false);
        setWebhookPreview(null);
      }
      void queryClient.invalidateQueries({ queryKey: ["/api/meetings", variables.id, "deliveries"] });
      toast({ title: t("Webhook delivered") });
    },
    onError: (error, variables) => {
      if (variables.id === selectedId) {
        toast({ variant: "destructive", title: t("Webhook delivery failed"), description: t(error.message) });
      }
    },
  });
  const meetingImportMutation = useMutation({
    mutationFn: async ({ file, title, profile }: { file: File; title: string; profile: MeetingProviderProfile }) => {
      meetingImportExplicitCancelRef.current = false;
      const createResponse = await fetchWithTimeout(apiUrl("/api/meeting-imports"), {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          filename: file.name,
          byteSize: file.size,
          title: title.trim() || file.name.replace(/\.[^.]+$/, ""),
          language: profile.language || "auto",
          profileId: profile.id,
        }),
      }, 30_000);
      const created = await createResponse.json() as MeetingImportJob & { message?: string };
      if (!createResponse.ok) throw new Error(created.message || `Meeting import could not be created (${createResponse.status})`);
      setMeetingImportId(created.id);
      meetingImportIdRef.current = created.id;
      upsertMeetingImportJob(queryClient, created);
      setMeetingImportProgress({
        importId: created.id,
        phase: "receiving",
        stage: "Uploading recording",
        percentage: 2,
      });
      return new Promise<MeetingImportJob>((resolve, reject) => {
        const request = new XMLHttpRequest();
        request.open("PUT", apiUrl(created.uploadUrl || `/api/meeting-imports/${created.id}/content`));
        request.withCredentials = true;
        request.timeout = 10 * 60 * 1000;
        request.setRequestHeader("Content-Type", file.type || "application/octet-stream");
        request.upload.onprogress = (event) => {
          if (!event.lengthComputable) return;
          const uploadProgress = Math.max(2, Math.min(85, Math.round((event.loaded / event.total) * 85)));
          setMeetingImportProgress((current) => mergeMeetingImportProgress(current, {
            importId: created.id,
            phase: "receiving",
            stage: "Uploading recording",
            percentage: uploadProgress,
          }));
        };
        request.upload.onload = () => setMeetingImportProgress((current) => mergeMeetingImportProgress(current, {
          importId: created.id,
          phase: "receiving",
          stage: "Safely committing upload",
          percentage: 85,
        }));
        request.onerror = () => reject(new Error("The meeting recording upload was interrupted."));
        request.ontimeout = () => reject(new Error("The meeting recording import timed out."));
        request.onabort = () => reject(new DOMException("Meeting import cancelled", "AbortError"));
        request.onload = () => {
          let payload: (MeetingImportJob & { message?: string }) | null = null;
          try {
            payload = JSON.parse(request.responseText) as MeetingImportJob & { message?: string };
          } catch {
            reject(new Error(`Meeting import failed (${request.status || "network"})`));
            return;
          }
          if (request.status < 200 || request.status >= 300) {
            reject(new Error(payload.message || `Meeting import failed (${request.status})`));
            return;
          }
          setMeetingImportProgress((current) => mergeMeetingImportProgress(current, {
            importId: created.id,
            phase: "received",
            stage: "Upload safely stored",
            percentage: 86,
          }));
          resolve(payload);
        };
        request.send(file);
      });
    },
    onSuccess: (job) => {
      upsertMeetingImportJob(queryClient, job);
      toast({
        title: t("Recording safely uploaded"),
        description: t("Scriber is preparing the transcript, speaker names, playback, and summary."),
      });
    },
    onError: (error) => {
      const importId = meetingImportIdRef.current;
      if (
        meetingImportExplicitCancelRef.current
        || (error instanceof DOMException && error.name === "AbortError")
      ) {
        invalidateMeetingImports();
        setMeetingImportProgress((current) => mergeMeetingImportProgress(current, {
          importId: importId || current.importId,
          phase: "cancel_requested",
          stage: "Cancel requested",
          percentage: 0,
        }));
        return;
      }
      meetingImportIdRef.current = "";
      setMeetingImportId("");
      setMeetingImportCandidate(null);
      invalidateMeetingImports();
      toast({
        variant: "destructive",
        title: t("Meeting import needs attention"),
        description: importId
          ? t("The upload may still have finished. Check Imports in a moment; Scriber will continue from the saved copy when possible.")
          : t(error.message),
      });
    },
  });

  const meetingImportCancelMutation = useMutation({
    onMutate: (importId: string) => {
      if (meetingImportIdRef.current === importId) {
        meetingImportExplicitCancelRef.current = true;
      }
    },
    mutationFn: async (importId: string) => {
      const response = await apiRequest("DELETE", `/api/meeting-imports/${importId}`);
      return response.json() as Promise<MeetingImportJob>;
    },
    onSuccess: (job) => {
      upsertMeetingImportJob(queryClient, job);
      if (
        meetingImportIdRef.current === job.id
        || (job.state === "canceled" && !meetingImportIdRef.current)
      ) {
        meetingImportIdRef.current = "";
        meetingImportExplicitCancelRef.current = false;
        setMeetingImportId("");
        setMeetingImportCandidate(null);
      }
      toast({ title: job.state === "canceled" ? t("Meeting import canceled") : t("Cancellation requested") });
    },
    onError: (error) => {
      meetingImportExplicitCancelRef.current = false;
      invalidateMeetingImports();
      toast({ variant: "destructive", title: t("Meeting import could not be canceled"), description: t(error.message) });
    },
  });

  const controlMutation = useMutation({
    mutationFn: async ({ id, action }: { id: string; action: "pause" | "resume" | "stop" }) => {
      const response = await apiRequest("POST", `/api/meetings/${id}/${action}`);
      return response.json() as Promise<MeetingSummary>;
    },
    onSuccess: (meeting, variables) => {
      applyMeetingSummaryEvent(queryClient, meeting);
      if (variables.action === "stop") {
        void queryClient.invalidateQueries({
          queryKey: ["/api/meetings", meeting.id],
          exact: true,
        });
      }
    },
    onError: (error) => toast({ variant: "destructive", title: t("Meeting control failed"), description: t(error.message) }),
  });

  const deleteMeetingMutation = useMutation({
    mutationFn: async (id: string) => {
      await apiRequest("DELETE", `/api/meetings/${id}`);
      return id;
    },
    onSuccess: (id) => {
      setMeetingPendingDelete(null);
      queryClient.removeQueries({ queryKey: ["/api/meetings", id] });
      void refreshMeetingCollections(queryClient);
      void refreshMeetingCapabilities(queryClient);
      if (selectedId === id) setLocation("/meetings");
      toast({ title: t("Meeting deleted"), description: t("Transcript, generated outputs, and locally retained audio were removed.") });
    },
    onError: (error) => toast({ variant: "destructive", title: t("Meeting could not be deleted"), description: t(error.message) }),
  });

  const exportMutation = useMutation({
    mutationFn: async ({ path, fallbackName }: { path: string; fallbackName: string }) => (
      saveMeetingExport(path, fallbackName)
    ),
    onSuccess: (result) => {
      if (result.status === "cancelled") return;
      setLastExport(result);
      setEmailDialogOpen(false);
      if (!result.desktop) {
        toast({
          title: t("Download started"),
          description: t("{{filename}} will appear in your browser's Downloads folder.", { filename: result.filename }),
        });
      }
    },
    onError: (error) => toast({ variant: "destructive", title: t("Export failed"), description: t(error.message) }),
  });

  const runSavedExportAction = useCallback(async (action: "open" | "reveal") => {
    if (!lastExport?.desktop) return;
    try {
      if (action === "open") await openMeetingExport(lastExport.token);
      else await revealMeetingExport(lastExport.token);
    } catch (error) {
      toast({
        variant: "destructive",
        title: action === "open" ? t("File could not be opened") : t("Folder could not be opened"),
        description: t(error instanceof Error ? error.message : String(error)),
      });
    }
  }, [lastExport, t, toast]);

  const composeEmailBody = useCallback(async () => {
    const preview = emailPreviewQuery.data;
    if (!preview) return;
    const recipients = preview.recipients.map((item) => item.address).join(",");
    const url = `mailto:${recipients}?subject=${encodeURIComponent(preview.subject)}&body=${encodeURIComponent(preview.body)}`;
    try {
      const { openUrl } = await import("@tauri-apps/plugin-opener");
      await openUrl(url);
    } catch {
      window.location.href = url;
    }
  }, [emailPreviewQuery.data]);

  const noteMutation = useMutation({
    mutationFn: async ({ id, body }: { id: string; body: string }) => {
      const response = await apiRequest("PUT", `/api/meetings/${id}/notes`, { id: "workspace", body });
      return response.json() as Promise<MeetingNote>;
    },
    onSuccess: (_payload, variables) => {
      if (noteDraftRef.current.meetingId === variables.id) {
        lastSavedNote.current = variables.body;
        noteDraftRef.current.savedBody = variables.body;
      }
      applyMeetingNoteEvent(queryClient, variables.id, _payload);
    },
    onError: (error) => toast({ variant: "destructive", title: t("Note was not saved"), description: t(error.message) }),
  });
  useEffect(() => {
    if (!selectedId || noteHydratedFor !== selectedId) return;
    const body = note.trim();
    noteDraftRef.current = { meetingId: selectedId, body, savedBody: noteDraftRef.current.savedBody };
    if (body === lastSavedNote.current || noteMutation.isPending) return;
    const handle = window.setTimeout(() => noteMutation.mutate({ id: selectedId, body }), 700);
    return () => window.clearTimeout(handle);
  }, [note, noteHydratedFor, selectedId, noteMutation.isPending]);
  useEffect(() => () => {
    const draft = noteDraftRef.current;
    if (draft.meetingId && draft.body !== draft.savedBody) {
      void apiRequest("PUT", `/api/meetings/${draft.meetingId}/notes`, {
        id: "workspace",
        body: draft.body,
      }).catch((error) => {
        toast({ variant: "destructive", title: t("Note was not saved"), description: t(error.message) });
      });
    }
  }, [selectedId, t, toast]);
  const actionItemMutation = useMutation({
    scope: { id: "meeting-action-item-updates" },
    mutationFn: async ({ id, itemId, changes }: { id: string; itemId: string; changes: Record<string, unknown> }) => {
      const response = await apiRequest("PATCH", `/api/meetings/${id}/action-items/${itemId}`, changes);
      return response.json() as Promise<MeetingActionItem>;
    },
    onSuccess: (item, variables) => {
      applyMeetingActionItem(queryClient, variables.id, item);
    },
    onError: (error) => toast({ variant: "destructive", title: t("Action item was not saved"), description: t(error.message) }),
  });
  const analysisMutation = useMutation({
    mutationFn: async (id: string) => {
      const response = await apiRequest("POST", `/api/meetings/${id}/analyze`);
      return response.json() as Promise<MeetingSummary>;
    },
    onSuccess: (meeting) => {
      applyMeetingSummaryEvent(queryClient, meeting);
      void queryClient.invalidateQueries({
        queryKey: ["/api/meetings", meeting.id],
        exact: true,
      });
    },
    onError: (error) => toast({ variant: "destructive", title: t("Analysis could not start"), description: t(error.message) }),
  });
  const segmentEditMutation = useMutation({
    mutationFn: async ({
      segment,
      action,
      text,
    }: {
      segment: MeetingSegment;
      action: "edit" | "undo";
      text?: string;
    }) => {
      const path = action === "undo"
        ? `/api/meetings/${segment.meetingId}/segments/${segment.id}/undo`
        : `/api/meetings/${segment.meetingId}/segments/${segment.id}`;
      const response = await apiRequest(
        action === "undo" ? "POST" : "PATCH",
        path,
        {
          expectedEditVersion: detail?.transcriptEditVersion ?? 0,
          ...(action === "edit" ? { text: text ?? "" } : {}),
        },
      );
      return response.json() as Promise<{
        meetingId: string;
        segment: MeetingSegment;
        transcriptEditVersion: number;
        outputsStale: boolean;
      }>;
    },
    onSuccess: (result) => {
      applyMeetingTranscriptEditedEvent(
        queryClient,
        result.meetingId,
        result.segment,
        result.transcriptEditVersion,
      );
      toast({
        title: t("Transcript corrected"),
        description: result.outputsStale
          ? t("The existing meeting brief is marked as based on an older transcript.")
          : t("Search, playback links, and new exports now use the correction."),
      });
    },
    onError: (error) => toast({
      variant: "destructive",
      title: t("Transcript correction was not saved"),
      description: t(error.message),
    }),
  });
  const recoveryMutation = useMutation({
    mutationFn: async ({ id, action, finalProvider }: { id: string; action: "retry" | "discard"; finalProvider?: string }) => {
      const response = await apiRequest(
        "POST",
        `/api/meetings/${id}/${action}`,
        action === "retry" && finalProvider ? { finalProvider } : undefined,
      );
      return response.json() as Promise<MeetingSummary>;
    },
    onSuccess: (meeting, variables) => {
      applyMeetingSummaryEvent(queryClient, meeting);
      invalidateMeetingImports();
      if (variables.action === "discard") setLocation("/meetings");
    },
    onError: (error) => toast({ variant: "destructive", title: t("Meeting could not be recovered"), description: t(error.message) }),
  });
  const reprocessMutation = useMutation({
    mutationFn: async ({ id, mode }: { id: string; mode: MeetingReprocessMode }) => {
      const response = await apiRequest("POST", `/api/meetings/${id}/reprocess`, { mode });
      return response.json() as Promise<{
        apiVersion: string;
        meeting: MeetingSummary;
        mode: MeetingReprocessMode;
      }>;
    },
    onSuccess: (payload) => {
      applyMeetingSummaryEvent(queryClient, payload.meeting);
      void queryClient.invalidateQueries({ queryKey: ["/api/meetings", payload.meeting.id], exact: true });
      void queryClient.invalidateQueries({ queryKey: ["/api/meetings", payload.meeting.id, "speaker-assignments"], exact: true });
      void queryClient.invalidateQueries({ queryKey: ["/api/meetings", payload.meeting.id, "email-preview"], exact: true });
      setReprocessDialogOpen(false);
      setWorkspaceView("transcript");
      toast({
        title: payload.mode === "speaker_identity" ? t("Speaker matches are being refreshed") : t("A new full transcript is being created"),
        description: t("The original recording remains unchanged."),
      });
    },
    onError: (error) => toast({ variant: "destructive", title: t("Meeting could not be processed again"), description: t(error.message) }),
  });
  const speakerMutation = useMutation({
    mutationFn: async ({ id, speakerId, displayName }: { id: string; speakerId: string; displayName: string }) => {
      const response = await apiRequest("PATCH", `/api/meetings/${id}/speakers/${speakerId}`, { displayName });
      return response.json() as Promise<{ apiVersion: string; success: boolean }>;
    },
    onSuccess: (_payload, variables) => {
      applyMeetingSpeakerName(queryClient, variables.id, variables.speakerId, variables.displayName);
    },
    onError: (error) => toast({ variant: "destructive", title: t("Speaker name was not saved"), description: t(error.message) }),
  });
  const speakerProfileNameMutation = useMutation({
    mutationFn: async ({ profileId, displayName }: { profileId: string; displayName: string }) => {
      const response = await apiRequest("PATCH", `/api/meetings/speaker-profiles/${profileId}`, { displayName });
      return response.json() as Promise<{
        apiVersion: string;
        id: string;
        displayName: string;
        isNamed: boolean;
        updatedAt: string;
      }>;
    },
    onSuccess: (profile) => {
      // Settings reads this exact query key. Update it synchronously so a user
      // who navigates there immediately sees the name they just saved here.
      queryClient.setQueryData<SpeakerProfilesResponse>(
        ["/api/meetings/speaker-profiles"],
        (current) => current ? {
          ...current,
          items: current.items.map((item) => item.id === profile.id
            ? { ...item, displayName: profile.displayName, isNamed: true, updatedAt: profile.updatedAt }
            : item),
        } : current,
      );
      if (detail?.id) {
        queryClient.setQueryData<MeetingSpeakerAssignmentsResponse>(
          ["/api/meetings", detail.id, "speaker-assignments"],
          (current) => current ? {
            ...current,
            items: current.items.map((item) => item.profileMatch?.profileId === profile.id
              ? { ...item, profileMatch: { ...item.profileMatch, displayName: profile.displayName } }
              : item),
          } : current,
        );
      }
      void queryClient.invalidateQueries({ queryKey: ["/api/meetings/speaker-profiles"], exact: true });
      if (detail?.id) void refreshMeetingDetail(queryClient, detail.id);
      toast({ title: t("Saved voice name updated"), description: t("{{name}} will be used for future voice matches.", { name: profile.displayName }) });
    },
    onError: (error) => toast({ variant: "destructive", title: t("Saved voice name was not updated"), description: t(error.message) }),
  });
  const splitSpeakerMutation = useMutation({
    mutationFn: async ({ id, speakerId }: { id: string; speakerId: string }) => {
      const response = await apiRequest("POST", `/api/meetings/${id}/speakers/${speakerId}/split-profile`);
      return response.json() as Promise<{ meetingId: string; speakerId: string; oldProfileId: string; newProfileId: string }>;
    },
    onSuccess: (_payload, variables) => {
      applyMeetingSpeakerProfileSplit(queryClient, variables.id, variables.speakerId);
      void refreshMeetingDetail(queryClient, variables.id);
      void queryClient.invalidateQueries({ queryKey: ["/api/meetings/speaker-profiles"] });
      toast({ title: t("Speaker will be recognized separately") });
    },
    onError: (error) => toast({ variant: "destructive", title: t("Speaker could not be separated"), description: t(error.message) }),
  });
  const chatMutation = useMutation({
    mutationFn: async ({ id, question }: { id: string; question: string }) => {
      const response = await apiRequest("POST", `/api/meetings/${id}/chat`, { question });
      return response.json() as Promise<{ message: { content: string; citations: string[] } }>;
    },
    onSuccess: (payload, variables) => {
      if (variables.id !== selectedId) return;
      setChatAnswer(payload.message);
      setChatQuestion("");
    },
    onError: (error, variables) => {
      if (variables.id === selectedId) {
        toast({ variant: "destructive", title: t("Meeting chat failed"), description: t(error.message) });
      }
    },
  });

  const meetings = useMemo(() => {
    const seen = new Set<string>();
    return (meetingsQuery.data?.pages ?? []).flatMap((page) => page.items).filter((meeting) => {
      if (seen.has(meeting.id)) return false;
      seen.add(meeting.id);
      return true;
    });
  }, [meetingsQuery.data?.pages]);
  const meetingsTotal = meetingsQuery.data?.pages[0]?.total ?? meetings.length;
  const activeMeeting = meetingsQuery.data?.pages.find((page) => page.activeMeeting)?.activeMeeting ?? null;
  const selectedProfile = profilesQuery.data?.profiles.find((item) => item.id === profilesQuery.data?.defaultProfileId)
    ?? profilesQuery.data?.profiles[0];
  const selectedProfileCostPerHour = selectedProfile?.costEstimate?.totalPerMeetingHour;
  const audioDeviceInventory = audioDevicesQuery.data;
  const audioDeviceInitialLoading = audioDevicesQuery.isPending;
  const audioDeviceInventoryUnavailable = audioDevicesQuery.isError
    || Boolean(audioDeviceInventory && !audioDeviceInventory.available);
  const captureEndpoints = audioDeviceInventory?.capture ?? [];
  const renderEndpoints = audioDeviceInventory?.render ?? [];
  const microphoneCountLabel = t(captureEndpoints.length === 1 ? "{{count}} microphone" : "{{count}} microphones", { count: formatNumber(captureEndpoints.length) });
  const speakerCountLabel = t(renderEndpoints.length === 1 ? "{{count}} speaker choice" : "{{count}} speaker choices", { count: formatNumber(renderEndpoints.length) });
  const microphoneSelectDisabled = audioDeviceInitialLoading
    || audioDeviceInventoryUnavailable
    || captureEndpoints.length === 0;
  const renderSelectDisabled = audioDeviceInitialLoading
    || audioDeviceInventoryUnavailable
    || renderEndpoints.length === 0;
  const audioDeviceStatus = audioDeviceInitialLoading
    ? t("Looking for microphones and speakers…")
    : audioDevicesQuery.isError
      ? t("The device list could not be loaded. Scriber will keep the Windows defaults; choose Refresh to try again.")
      : !audioDeviceInventory?.available
        ? t("Individual device selection is unavailable. Scriber will use the Windows default microphone and speakers.")
        : captureEndpoints.length === 0 && renderEndpoints.length === 0
          ? t("No individual audio devices were returned. Scriber will use the Windows defaults.")
          : captureEndpoints.length === 0
            ? t("No individual microphones were returned. Windows default will be used · {{speakers}} available.", { speakers: speakerCountLabel })
            : renderEndpoints.length === 0
              ? t("{{microphones}} available · Windows default speakers will be used.", { microphones: microphoneCountLabel })
              : t("{{microphones}} and {{speakers}} available.", { microphones: microphoneCountLabel, speakers: speakerCountLabel });
  const longSession = capabilitiesQuery.data?.longSession;
  const finalProviderCapability = selectedProfile
    ? profilesQuery.data?.providerCapabilities[selectedProfile.finalProvider]
    : undefined;
  const longSessionReady = Boolean(
    capabilitiesQuery.data?.nativeMeetingCapture
      && longSession?.storageReady
      && finalProviderCapability?.fiveHourSupported,
  );
  const finalProviderDurationLabel = finalProviderCapability?.maxDurationSeconds != null
    ? t("Up to {{duration}}", { duration: formatImportDuration(finalProviderCapability.maxDurationSeconds, t) })
    : finalProviderCapability?.fiveHourSupported
      ? t("Ready for 5 hours")
      : t("Not for 5-hour meetings");
  const meetingImportProfile = selectedProfile;
  const meetingImportFinalCostPerAudioHour = meetingImportProfile?.costEstimate?.singleTrackFinalPerAudioHour;
  const meetingImportFinalProviderCapability = meetingImportProfile
    ? profilesQuery.data?.providerCapabilities[meetingImportProfile.finalProvider]
    : undefined;
  const meetingImportExceedsProviderDuration = Boolean(
    meetingImportCandidate?.durationSeconds != null
      && meetingImportFinalProviderCapability?.maxDurationSeconds != null
      && meetingImportCandidate.durationSeconds > meetingImportFinalProviderCapability.maxDurationSeconds,
  );
  const detailLivePreview = detail?.captureMetadata.livePreview;
  const detailLiveModel = (
    detailLivePreview
    && typeof detailLivePreview === "object"
    && "model" in detailLivePreview
  ) ? String((detailLivePreview as Record<string, unknown>).model || "") : "";
  const detailFinalProviderCapability = detail
    ? profilesQuery.data?.providerCapabilities[detail.finalProvider]
    : undefined;
  const startBlocked = Boolean(
    !capabilitiesQuery.data?.nativeMeetingCapture || capabilitiesQuery.data?.liveMicBusy || activeMeeting
      || !selectedProfile?.available || calendarSelectionNeedsReview,
  );
  const meetingStartStatus = startMutation.isPending
    ? t("Starting audio capture…")
    : activeMeeting
      ? t("Finish “{{title}}” first.", { title: activeMeeting.title })
      : calendarSelectionNeedsReview
        ? t("Choose the Outlook meeting again or continue without Outlook.")
        : !capabilitiesQuery.data?.nativeMeetingCapture
          ? t("Meeting recording is unavailable on this PC.")
          : capabilitiesQuery.data?.liveMicBusy
            ? t("Finish the Live Mic recording first.")
            : !selectedProfile?.available
              ? t("Choose an available transcription option in Settings.")
              : longSessionReady
                ? t("Ready to record · safety saves every {{seconds}} seconds", { seconds: formatNumber(longSession?.checkpointIntervalSeconds ?? 30) })
                : t("Ready for a shorter meeting · review the details before a long recording.");
  const meetingImportBusy = meetingImportMutation.isPending || Boolean(meetingImportId);
  const liveSegments = detail?.segments ?? [];
  const groupedSegments = useMemo(() => liveSegments.map((segment) => {
    const rawLabel = segment.speakerLabel
      || (segment.source === "microphone" ? "You" : "Meeting audio");
    return {
      ...segment,
      label: rawLabel === "You" || rawLabel === "Meeting audio" ? t(rawLabel) : rawLabel,
    };
  }), [liveSegments, t]);
  const visibleTranscriptSegments = useMemo(() => {
    const query = transcriptSearch.trim().toLocaleLowerCase(localeTag);
    if (!query) return groupedSegments;
    return groupedSegments.filter((segment) => (
      segment.text.toLocaleLowerCase(localeTag).includes(query)
      || segment.label.toLocaleLowerCase(localeTag).includes(query)
    ));
  }, [groupedSegments, localeTag, transcriptSearch]);
  const analysisOutput = detail?.outputs.find((output) => output.kind === "analysis" && output.status === "completed");
  const currentAnalysisOutput = detail?.outputs.find((output) => (
    output.kind === "analysis"
    && output.status === "completed"
    && output.transcriptEditVersion === (detail.transcriptEditVersion ?? 0)
  ));
  const technicalAnalysisModel = currentAnalysisOutput
    ? t("{{provider}} · output v{{version}}", { provider: currentAnalysisOutput.provider || t("Model not recorded"), version: formatNumber(currentAnalysisOutput.version) })
    : analysisOutput
      ? t("{{provider}} · previous output v{{version}}", { provider: analysisOutput.provider || t("Model not recorded"), version: formatNumber(analysisOutput.version) })
      : t("Not generated");
  const analysis = analysisOutput?.payload;
  const hasCanonicalTranscript = Boolean(detail?.segments.some((segment) => segment.revision === "canonical"));
  const outputsStale = Boolean(detail?.outputs.some(
    (output) => output.status === "completed"
      && (output.transcriptEditVersion ?? 0) < (detail.transcriptEditVersion ?? 0),
  ));
  const generateAnalysis = () => {
    if (!detail) return;
    if (!hasCanonicalTranscript) {
      toast({
        variant: "destructive",
        title: t("Transcript is not ready"),
        description: t("Wait for the final transcript before generating the meeting brief."),
      });
      return;
    }
    analysisMutation.mutate(detail.id);
  };
  const playbackMix = detail?.audioAssets.find((asset) => asset.kind === "playback_mix") ?? null;
  const hasPlayableAudio = Boolean(playbackMix);
  const playbackMixOriginMs = meetingPlaybackOriginMs(detail?.audioAssets, "mix");
  const fallbackPlaybackMixEndMs = liveSegments.reduce(
    (endMs, segment) => Math.max(endMs, segment.endMs),
    playbackMixOriginMs,
  );
  const playbackMixEndMs = playbackMix?.durationMs == null
    ? fallbackPlaybackMixEndMs
    : playbackMixOriginMs + playbackMix.durationMs;
  const canPlaySpeakerSamples = hasPlayableAudio
    && playbackMixEndMs - playbackMixOriginMs >= MEETING_SPEAKER_SAMPLE_MIN_MS;
  const playableSpeakerIds = useMemo(() => new Set(
    canPlaySpeakerSamples
      ? liveSegments.filter((segment) => segment.speakerId && segment.endMs > segment.startMs).map((segment) => segment.speakerId!)
      : [],
  ), [canPlaySpeakerSamples, liveSegments]);
  const aecMetrics = detail?.captureMetadata.aecMetrics;
  const latestCheckpoint = detail?.transcriptCheckpoints?.at(-1);
  const expectedCheckpointTrackCount = useMemo(() => {
    const sources = detail?.captureMetadata.sources;
    if (Array.isArray(sources)) {
      const known = new Set(sources.filter((source) => (
        source === "microphone" || source === "mic_clean" || source === "system"
      )));
      if (known.size > 0) return known.size;
    }
    return detail?.aecEnabled ? 3 : 2;
  }, [detail?.aecEnabled, detail?.captureMetadata.sources]);
  const playLoadedAudio = useCallback(async (request: MeetingPlaybackRequest) => {
    const audio = audioRef.current;
    if (!audio) return;
    try {
      audio.currentTime = meetingTimeToAssetTimeSeconds(
        request.meetingTimeMs,
        detail?.audioAssets,
        "mix",
      );
      if (request.shouldPlay) {
        await audio.play();
      } else {
        audio.pause();
      }
      setPlaybackError("");
    } catch (error) {
      setPlaybackError(error instanceof Error ? error.message : "Audio playback could not start.");
    }
  }, [detail?.audioAssets]);
  const captureCurrentPlayback = useCallback((): MeetingPlaybackRequest => {
    const audio = audioRef.current;
    if (!audio) return { meetingTimeMs: 0, shouldPlay: false };
    return captureMeetingPlaybackRequest(
      audio.currentTime,
      audio.paused,
      audio.ended,
      detail?.audioAssets,
      "mix",
    );
  }, [detail?.audioAssets]);
  const playSegment = useCallback((startMs: number) => {
    if (!detail) return;
    speakerSnippetEndMsRef.current = null;
    savedVoicePreviewRef.current?.pause();
    savedVoicePreviewProfileIdRef.current = "";
    if (!hasPlayableAudio) {
      setPlaybackError("Saved audio is not available for this meeting.");
      return;
    }
    const request: MeetingPlaybackRequest = {
      meetingTimeMs: Math.max(0, startMs),
      shouldPlay: true,
    };
    setPlaybackError("");
    if ((audioRef.current?.readyState ?? 0) >= 1) {
      pendingPlaybackRef.current = null;
      void playLoadedAudio(request);
    } else {
      pendingPlaybackRef.current = request;
      audioRef.current?.load();
    }
  }, [detail, hasPlayableAudio, playLoadedAudio]);
  const playSpeakerSnippet = useCallback((speakerId: string) => {
    const segment = [...liveSegments]
      .filter((item) => item.speakerId === speakerId && item.endMs > item.startMs)
      .sort((left, right) => {
        if (left.revision !== right.revision) return left.revision === "canonical" ? -1 : 1;
        return right.durationMs - left.durationMs;
      })[0];
    if (!segment) {
      setPlaybackError("No saved voice sample is available for this speaker.");
      return;
    }
    const sampleWindow = meetingSpeakerSampleWindow(
      segment.startMs,
      segment.endMs,
      playbackMixOriginMs,
      playbackMixEndMs - playbackMixOriginMs,
    );
    if (!sampleWindow) {
      setPlaybackError("No saved voice sample of at least 5 seconds is available for this speaker.");
      return;
    }
    playSegment(sampleWindow.startMs);
    speakerSnippetEndMsRef.current = sampleWindow.endMs;
  }, [liveSegments, playbackMixEndMs, playbackMixOriginMs, playSegment]);
  const focusSpeakerAssignment = useCallback((speakerId: string) => {
    speakerAssignmentRequestIdRef.current += 1;
    setSpeakerAssignmentRequest({
      speakerId,
      requestId: speakerAssignmentRequestIdRef.current,
    });
  }, []);
  const playSavedVoicePreview = useCallback(async (profileId: string, previewUrl: string) => {
    if (!previewUrl) return;
    speakerSnippetEndMsRef.current = null;
    audioRef.current?.pause();
    let preview = savedVoicePreviewRef.current;
    if (preview && savedVoicePreviewProfileIdRef.current === profileId && !preview.paused) {
      preview.pause();
      savedVoicePreviewProfileIdRef.current = "";
      return;
    }
    if (!preview) {
      preview = new Audio();
      preview.preload = "metadata";
      savedVoicePreviewRef.current = preview;
    } else {
      preview.pause();
    }
    savedVoicePreviewProfileIdRef.current = profileId;
    preview.src = apiUrl(previewUrl);
    preview.currentTime = 0;
    preview.onended = () => {
      if (savedVoicePreviewProfileIdRef.current === profileId) savedVoicePreviewProfileIdRef.current = "";
    };
    preview.onerror = () => {
      if (savedVoicePreviewProfileIdRef.current === profileId) savedVoicePreviewProfileIdRef.current = "";
      setPlaybackError("The saved voice sample could not be played.");
      void queryClient.invalidateQueries({ queryKey: ["/api/meetings/speaker-profiles"], exact: true });
    };
    try {
      await preview.play();
      setPlaybackError("");
    } catch (error) {
      savedVoicePreviewProfileIdRef.current = "";
      setPlaybackError(error instanceof Error ? error.message : "The saved voice sample could not be played.");
    }
  }, [queryClient]);
  useEffect(() => {
    const audio = audioRef.current;
    if (!pendingPlaybackRef.current || !audio) return;
    audio.load();
  }, [detail?.id]);
  const handlePlaybackMetadata = useCallback(() => {
    const request = pendingPlaybackRef.current;
    if (!request) return;
    pendingPlaybackRef.current = null;
    void playLoadedAudio(request);
  }, [playLoadedAudio]);
  const handlePlaybackTimeUpdate = useCallback(() => {
    const stopAtMs = speakerSnippetEndMsRef.current;
    if (stopAtMs == null) return;
    if (captureCurrentPlayback().meetingTimeMs < stopAtMs) return;
    speakerSnippetEndMsRef.current = null;
    audioRef.current?.pause();
  }, [captureCurrentPlayback]);
  const seekCitation = useCallback((segmentId: string) => {
    const segment = liveSegments.find((item) => item.id === segmentId);
    if (segment) playSegment(segment.startMs);
  }, [liveSegments, playSegment]);
  const openMeetingSettings = useCallback(() => {
    try {
      window.sessionStorage.setItem("scriber:open-settings-section", "meetings");
    } catch {
      // Settings still opens when session storage is unavailable.
    }
    setLocation("/settings");
  }, [setLocation]);
  const openReprocessDialog = useCallback(() => {
    const availability = detail?.reprocessing;
    reprocessMutation.reset();
    setReprocessMode(
      availability?.speakerIdentityAvailable === false && availability.fullTranscriptAvailable
        ? "full_transcript"
        : "speaker_identity",
    );
    setReprocessDialogOpen(true);
  }, [detail?.reprocessing, reprocessMutation]);

  return (
    <div className="app-page-shell transcription-page meetings-page flex min-h-[calc(100dvh-3.5rem)] flex-col px-4 py-5 md:px-6 md:py-6" data-page-shell="meetings">
      <PageIntro
        eyebrow={t("Meeting workspace · 02")}
        title={t("Meetings")}
        description={t("Record, review, summarize, and follow up in one place.")}
        sticky={false}
        titleAccessory={activeMeeting ? <Badge variant="outline" className={stateTone(activeMeeting.state)}>{stateLabel(activeMeeting.state, t)}</Badge> : null}
        actions={!selectedId ? <>
          <input
            ref={meetingImportRef}
            type="file"
            accept="audio/*,video/*,.m4a,.m4v,.mkv,.webm,.opus,.flac,.wav,.mp3,.mp4,.mov,.avi"
            className="sr-only"
            aria-label={t("Import meeting recording")}
            onChange={(event) => {
              const file = event.target.files?.[0];
              event.target.value = "";
              if (!file) return;
              const title = file.name.replace(/\.[^.]+$/, "");
              setMeetingImportCandidate({ file, title, durationSeconds: null });
              setMeetingImportProgress({
                importId: "",
                phase: "created",
                stage: "Ready",
                percentage: 0,
              });
              const objectUrl = URL.createObjectURL(file);
              const probe = document.createElement("audio");
              probe.preload = "metadata";
              probe.onloadedmetadata = () => {
                const durationSeconds = Number.isFinite(probe.duration) ? probe.duration : null;
                setMeetingImportCandidate((current) => current?.file === file ? { ...current, durationSeconds } : current);
                URL.revokeObjectURL(objectUrl);
              };
              probe.onerror = () => URL.revokeObjectURL(objectUrl);
              probe.src = objectUrl;
            }}
          />
          <Button
            type="button"
            variant="outline"
            disabled={meetingImportBusy || Boolean(activeMeeting)}
            onClick={() => meetingImportRef.current?.click()}
          >
            {meetingImportBusy
              ? <Loader2 className="animate-spin" />
              : <FileUp />}
            <span className="hidden sm:inline">{t("Import recording")}</span>
            <span className="sm:hidden">{t("Import")}</span>
          </Button>
        </> : null}
      />

      {!capabilitiesQuery.isLoading && !capabilitiesQuery.data?.nativeMeetingCapture && (
        <div className="mb-4 flex items-start gap-3 rounded-2xl border border-amber-300/60 bg-amber-500/10 px-4 py-3 text-sm text-amber-900 dark:text-amber-100" role="status">
          <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0" />
          <div>
            <p className="font-semibold">{t("Native meeting capture is not connected")}</p>
            <p className="mt-0.5 opacity-80">{t("Meeting recording requires the installed Windows app and its private audio sidecar. History and notes remain available.")}</p>
          </div>
        </div>
      )}

      <h2 className="sr-only">{t("Meeting workspace")}</h2>
      <div className="grid min-h-[680px] flex-1 gap-4 min-[1100px]:grid-cols-[232px_minmax(0,1fr)]">
        <aside className={`${selectedId ? "hidden min-[1100px]:block" : ""} meetings-history-rail rounded-[22px] p-2`}>
          <div className="flex items-center justify-between px-2 py-2">
            <div>
              <p className="text-sm font-semibold">{t("Meetings")}</p>
              <p className="text-xs text-muted-foreground">{t("{{count}} saved", { count: formatNumber(meetingsTotal) })}</p>
            </div>
            <Button type="button" size="icon" variant="outline" onClick={() => setLocation("/meetings")} aria-label={t("Create meeting")}>
              <Plus className="h-4 w-4" />
            </Button>
          </div>
          <MeetingImportInbox
            items={meetingImportsQuery.data?.items ?? []}
            loading={meetingImportsQuery.isLoading}
            error={meetingImportsQuery.isError}
            cancelingId={meetingImportCancelMutation.isPending ? meetingImportCancelMutation.variables : undefined}
            retryingMeetingId={recoveryMutation.isPending ? recoveryMutation.variables?.id : undefined}
            onCancel={(importId) => meetingImportCancelMutation.mutate(importId)}
            onRetry={(meetingId) => recoveryMutation.mutate({ id: meetingId, action: "retry" })}
            onOpen={(meetingId) => setLocation(`/meetings/${meetingId}`)}
            onRefresh={() => void meetingImportsQuery.refetch()}
          />
          <div className="mt-2 grid grid-cols-1 gap-1 sm:grid-cols-2 lg:grid-cols-3 min-[1100px]:block min-[1100px]:space-y-1">
            {meetingsQuery.isLoading && [0, 1, 2].map((item) => <div key={item} className="h-[64px] min-w-0 animate-pulse rounded-xl bg-muted/70" />)}
            {meetingsQuery.isError && <p className="px-2 py-5 text-sm text-destructive">{t("Meeting history could not be loaded.")}</p>}
            {!meetingsQuery.isLoading && !meetingsQuery.isError && meetings.length === 0 && (
              <div className="flex min-w-full items-center gap-3 px-3 py-4 text-left text-sm text-muted-foreground min-[1100px]:block min-[1100px]:py-10 min-[1100px]:text-center">
                <CalendarClock className="h-6 w-6 shrink-0 min-[1100px]:mx-auto min-[1100px]:mb-3 min-[1100px]:h-7 min-[1100px]:w-7" />
                {t("Your first meeting will appear here.")}
              </div>
            )}
            {meetings.map((meeting) => (
              <div
                key={meeting.id}
                className={`neu-nav-item group flex min-w-0 items-center rounded-[14px] px-1 py-1 min-[1100px]:w-full ${selectedId === meeting.id ? "neu-nav-active text-foreground" : "text-muted-foreground"}`}
              >
                <button
                  type="button"
                  onClick={() => setLocation(`/meetings/${meeting.id}`)}
                  className="min-w-0 flex-1 rounded-[10px] px-2 py-2 text-left outline-none focus-visible:ring-2 focus-visible:ring-primary"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate text-sm font-medium text-foreground">{meeting.title}</span>
                    {meeting.state === "recording" && <span className="h-2 w-2 shrink-0 animate-pulse rounded-full bg-red-500" />}
                  </div>
                  <div className="mt-1 flex items-center justify-between gap-2 text-xs text-muted-foreground">
                     <span>{formatMoment(meeting.createdAt, formatDate)}</span>
                     <span className="truncate">{stateLabel(meeting.state, t)}</span>
                  </div>
                </button>
                <button
                  type="button"
                  aria-label={t("Delete {{title}}", { title: meeting.title })}
                  title={OPEN_STATES.has(meeting.state) ? t("Stop this meeting before deleting it") : t("Delete meeting")}
                  disabled={OPEN_STATES.has(meeting.state)}
                  onClick={() => setMeetingPendingDelete(meeting)}
                  className={`mr-1 flex h-11 w-11 shrink-0 items-center justify-center rounded-lg text-muted-foreground outline-none hover:bg-destructive/10 hover:text-destructive focus-visible:ring-2 focus-visible:ring-primary disabled:pointer-events-none disabled:opacity-30 min-[1100px]:h-8 min-[1100px]:w-8 ${selectedId === meeting.id ? "opacity-100" : "opacity-100 min-[1100px]:pointer-events-none min-[1100px]:opacity-0 min-[1100px]:group-hover:pointer-events-auto min-[1100px]:group-hover:opacity-100 min-[1100px]:group-focus-within:pointer-events-auto min-[1100px]:group-focus-within:opacity-100"}`}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </div>
            ))}
            {meetingsQuery.hasNextPage && (
              <Button
                type="button"
                size="sm"
                variant="ghost"
                disabled={meetingsQuery.isFetchingNextPage}
                onClick={() => void meetingsQuery.fetchNextPage()}
                className="mt-1 w-full text-xs min-[1100px]:w-full"
              >
                {meetingsQuery.isFetchingNextPage && <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />}
                {t("Load older meetings")}
              </Button>
            )}
          </div>
        </aside>

        <main className="meetings-workspace-panel min-w-0 overflow-hidden rounded-[26px]">
          {!selectedId ? (
            <div className="h-full overflow-y-auto">
              <header className="border-b border-border/60 px-5 py-5 md:px-6 md:py-6 lg:px-7">
                <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-primary">{t("New meeting")}</p>
                <h2 className="mt-1 font-heading text-[26px] font-semibold leading-tight tracking-[-0.03em] md:text-[28px]">{t("Ready to start")}</h2>
                <p className="mt-2 max-w-[65ch] text-[13px] leading-5 text-muted-foreground md:text-[13.5px]">
                  {selectedProfile?.transcriptionMode === "final_only"
                    ? t("Check the title and choose which microphone and speakers to record. Scriber saves both on this device and creates the transcript after you stop.")
                    : t("Check the title and choose which microphone and speakers to record. Scriber saves both on this device while it shows live text.")}
                </p>
              </header>
              <div className="border-b border-border/60 bg-muted/20 px-5 py-3.5 md:px-6 lg:px-7">
                <div className="grid gap-3 sm:grid-cols-3 sm:gap-4">
                  {[
                    { icon: Mic2, label: "Microphone", detail: "Your voice" },
                    { icon: Headphones, label: "System audio", detail: "Other participants" },
                    { icon: Waves, label: "Echo control", detail: "Reduces speaker echo" },
                  ].map(({ icon: Icon, label, detail }) => (
                    <div key={label} className="flex min-w-0 items-center gap-2.5">
                      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-primary/10"><Icon className="h-4 w-4 text-primary" /></span>
                      <div><p className="text-sm font-medium">{t(label)}</p><p className="mt-0.5 text-xs text-muted-foreground">{t(detail)}</p></div>
                    </div>
                  ))}
                </div>
              </div>
              <div className="grid gap-5 p-5 md:p-6 lg:p-7 min-[1380px]:grid-cols-[minmax(0,1fr)_260px]">
                <section className="flex min-w-0 flex-col gap-4">
                <div>
                  <label htmlFor="meeting-title" className="text-sm font-medium">{t("Meeting title")}</label>
                  <Input id="meeting-title" value={title} onChange={(event) => setTitle(event.target.value)} placeholder={t("Weekly product sync")} className="mt-2 h-12 text-base" />
                  <div className="mt-3 flex flex-col gap-2 sm:flex-row sm:items-center">
                    <Button
                      type="button"
                      disabled={startBlocked || startMutation.isPending}
                      aria-describedby="meeting-start-status"
                      onClick={() => startMutation.mutate()}
                      className="w-full min-w-[168px] sm:w-auto"
                    >
                      {startMutation.isPending ? <Loader2 className="animate-spin" /> : <CirclePlay />}
                      {startMutation.isPending ? t("Starting…") : t("Start meeting")}
                    </Button>
                    <p id="meeting-start-status" className="flex min-h-5 items-center gap-1.5 text-xs leading-5 text-muted-foreground" role="status" aria-live="polite">
                      <ShieldCheck className={`h-3.5 w-3.5 shrink-0 ${startBlocked ? "text-amber-600 dark:text-amber-300" : "text-emerald-600 dark:text-emerald-300"}`} />
                      {meetingStartStatus}
                    </p>
                  </div>
                </div>
                {detectionQuery.data?.detection && <div className="rounded-2xl border border-primary/35 bg-primary/5 p-4">
                  <div className="flex items-start gap-3">
                    <MonitorSpeaker className="mt-0.5 h-5 w-5 shrink-0 text-primary" />
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-medium">{t("Meeting activity detected")}</p>
                      <p className="mt-1 truncate text-sm text-muted-foreground">{detectionQuery.data.detection.label}</p>
                      <div className="mt-3 flex flex-wrap gap-2">
                        <Button type="button" size="sm" variant="secondary" onClick={() => {
                          const calendarEvent = detectionQuery.data?.detection?.calendarEvent;
                          setTitle(calendarEvent?.subject || detectionQuery.data?.detection?.label || t("Meeting"));
                          if (calendarEvent?.id) {
                            setSelectedCalendarEventId(calendarEvent.id);
                            setCalendarSelectionNeedsReview(false);
                            selectedCalendarSubjectRef.current = calendarEvent.subject;
                          }
                        }}>{t("Use suggestion")}</Button>
                        <Button type="button" size="sm" variant="ghost" disabled={detectionDismissMutation.isPending} onClick={() => detectionDismissMutation.mutate(detectionQuery.data!.detection!.detectionId)}>{t("Dismiss")}</Button>
                      </div>
                    </div>
                  </div>
                </div>}
                <OutlookMeetingPicker
                  status={outlookQuery.data}
                  events={outlookEventsQuery.data}
                  statusLoading={outlookQuery.isLoading}
                  statusError={outlookQuery.isError || outlookQuery.data?.credentialStatusAvailable === false}
                  eventsLoading={outlookEventsQuery.isLoading || Boolean(outlookQuery.data?.connected && !outlookQuery.data.lastSyncAt && !outlookQuery.data.lastError)}
                  eventsError={outlookEventsQuery.isError || Boolean(outlookQuery.data?.connected && !outlookQuery.data.lastSyncAt && outlookQuery.data.lastError)}
                  refreshing={outlookSyncMutation.isPending || outlookQuery.isFetching || outlookEventsQuery.isFetching}
                  selectedEventId={selectedCalendarEventId}
                  selectionNeedsReview={calendarSelectionNeedsReview}
                  onSelect={(event: OutlookCalendarEvent | null) => {
                    setSelectedCalendarEventId(event?.id ?? "");
                    setCalendarSelectionNeedsReview(false);
                    selectedCalendarSubjectRef.current = event?.subject ?? "";
                    if (event) setTitle(event.subject || t("Meeting"));
                  }}
                  onRefresh={() => {
                    if (outlookQuery.data?.connected) outlookSyncMutation.mutate();
                    else void outlookQuery.refetch();
                  }}
                  onOpenSettings={openMeetingSettings}
                />
                <div className="grid min-w-0 gap-4 overflow-hidden rounded-2xl border border-border/70 bg-background/55 p-4">
                  <div className="min-w-0">
                    <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                      <div className="min-w-0">
                        <p className="text-sm font-medium">{t("Transcription")}</p>
                        <p className="mt-1 text-sm font-semibold text-foreground">
                          {selectedProfile
                            ? selectedProfile.transcriptionMode === "final_only"
                              ? t("Transcript after meeting")
                              : t("Live text + accurate transcript")
                            : t("Loading transcription settings…")}
                        </p>
                        {selectedProfile && <p className="mt-1 text-xs leading-5 text-muted-foreground">
                          {selectedProfile.transcriptionMode === "final_only"
                            ? t("No cloud live text · {{provider}} after you stop", { provider: selectedProfile.stages.find((stage) => stage.id === "final")?.provider ?? selectedProfile.finalProvider })
                            : selectedProfile.livePreviewAvailable === false
                              ? t("Live text needs a Soniox API key · {{provider}} still runs after you stop", { provider: selectedProfile.stages.find((stage) => stage.id === "final")?.provider ?? selectedProfile.finalProvider })
                              : t("Soniox live text · {{provider}} final pass", { provider: selectedProfile.stages.find((stage) => stage.id === "final")?.provider ?? selectedProfile.finalProvider })}
                          {selectedProfileCostPerHour != null
                            ? ` · ${t("about ${{cost}} per meeting hour", { cost: formatNumber(selectedProfileCostPerHour, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) })}`
                            : ` · ${t("provider price varies")}`}
                        </p>}
                      </div>
                      <Button type="button" size="sm" variant="outline" className="shrink-0" onClick={openMeetingSettings}>{t("Change in Settings")}</Button>
                    </div>
                    {selectedProfile && <details className="group mt-3 rounded-xl border border-border/60 bg-muted/15">
                      <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-3 py-2.5 text-xs font-medium marker:content-none">
                        <span>{t("What happens")}</span>
                        <ChevronDown className="h-3.5 w-3.5 text-muted-foreground group-open:rotate-180 motion-reduce:transform-none" />
                      </summary>
                      <div className="divide-y divide-border/60 border-t border-border/60 px-3">
                        {(selectedProfile.stages ?? []).map((stage) => <div key={stage.id} className="grid gap-1 py-2.5 sm:grid-cols-[130px_minmax(0,1fr)]">
                          <p className="text-[11px] font-medium text-muted-foreground">{t(stage.label)}</p>
                          <div className="min-w-0">
                            <p className="truncate text-xs font-semibold">{stage.provider}{stage.model ? <> · <span className="font-mono font-normal">{stage.model}</span></> : null}</p>
                            <p className="mt-0.5 text-[11px] leading-4 text-muted-foreground">{t(stage.purpose)}</p>
                          </div>
                        </div>)}
                      </div>
                    </details>}
                    {selectedProfile && !selectedProfile.available && <p className="mt-2 text-xs text-amber-700 dark:text-amber-300">{t(selectedProfile.unavailableReason)}</p>}
                  </div>
                  <div className="min-w-0 border-t border-border/60 pt-4">
                    <div className="flex items-center justify-between gap-3">
                      <p className="text-sm font-medium">{t("Audio devices")}</p>
                      <Button type="button" size="sm" variant="ghost" disabled={audioDevicesQuery.isFetching} onClick={() => void audioDevicesQuery.refetch()}><RefreshCw className={audioDevicesQuery.isFetching ? "animate-spin" : ""} />{t("Refresh")}</Button>
                    </div>
                    <div className="mt-2 grid gap-2 sm:grid-cols-2">
                      <div className="min-w-0"><label htmlFor="meeting-microphone" className="text-xs text-muted-foreground">{t("Microphone")}</label><select id="meeting-microphone" value={microphoneEndpointHash} disabled={microphoneSelectDisabled} onChange={(event) => setMicrophoneEndpointHash(event.target.value)} className="mt-1 h-9 w-full min-w-0 rounded-lg border border-input bg-background px-2 text-xs disabled:cursor-not-allowed disabled:opacity-60"><option value="">{audioDeviceInitialLoading ? t("Looking for microphones…") : captureEndpoints.length === 0 ? t("Windows default microphone (automatic)") : t("Windows default microphone")}</option>{captureEndpoints.map((endpoint) => <option key={endpoint.endpointIdHash} value={endpoint.endpointIdHash}>{endpoint.friendlyName}{endpoint.isDefault ? t(" (default)") : ""}</option>)}</select></div>
                      <div className="min-w-0"><label htmlFor="meeting-render" className="text-xs text-muted-foreground">{t("Speakers / meeting audio")}</label><select id="meeting-render" value={renderEndpointHash} disabled={renderSelectDisabled} onChange={(event) => setRenderEndpointHash(event.target.value)} className="mt-1 h-9 w-full min-w-0 rounded-lg border border-input bg-background px-2 text-xs disabled:cursor-not-allowed disabled:opacity-60"><option value="">{audioDeviceInitialLoading ? t("Looking for speakers…") : renderEndpoints.length === 0 ? t("Windows default speakers (automatic)") : t("Windows default speakers")}</option>{renderEndpoints.map((endpoint) => <option key={endpoint.endpointIdHash} value={endpoint.endpointIdHash}>{endpoint.friendlyName}{endpoint.isDefault ? t(" (default)") : ""}</option>)}</select></div>
                    </div>
                    <p className={`mt-2 text-xs leading-5 ${audioDeviceInventoryUnavailable ? "text-amber-700 dark:text-amber-300" : "text-muted-foreground"}`} role="status" aria-live="polite">{audioDeviceStatus}</p>
                    <Button type="button" size="sm" variant="outline" className="mt-3 h-auto min-h-9 w-full whitespace-normal px-3 text-center leading-5" disabled={!audioDevicesQuery.data?.available || deviceTestMutation.isPending || capabilitiesQuery.data?.liveMicBusy || Boolean(capabilitiesQuery.data?.activeMeeting)} onClick={() => deviceTestMutation.mutate()}>
                      {deviceTestMutation.isPending ? <Loader2 className="animate-spin" /> : <Waves />}{t("Test microphone and playback")}
                    </Button>
                    <p className="mt-2 text-[11px] leading-4 text-muted-foreground">{t("Speak normally during the 3-second test. Scriber also plays a short sound through your speakers. Nothing is saved or uploaded.")}</p>
                    {deviceTestMutation.data && <div className="mt-3 space-y-2" role="status">
                      {(["microphone", "system"] as const).map((source) => {
                        const result = deviceTestMutation.data?.sources[source];
                        const rms = result?.rms ?? 0;
                        const levelPercent = rms > 0
                          ? Math.max(3, Math.min(100, ((20 * Math.log10(rms) + 60) / 60) * 100))
                          : 0;
                        const hasFrames = (result?.frames ?? 0) > 0 && !result?.errorCode;
                        return <div key={source} className="rounded-lg border border-border/60 bg-muted/35 px-2.5 py-2">
                          <div className="flex items-center justify-between gap-3 text-[11px]"><span className="font-medium">{source === "microphone" ? t("Microphone input") : t("Speaker loopback")}</span><span className={hasFrames ? "text-emerald-700 dark:text-emerald-300" : "text-amber-700 dark:text-amber-300"}>{hasFrames ? t("Signal received") : result?.errorCode ? t("Could not read") : t("No signal")}</span></div>
                          <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-muted"><div className="h-full rounded-full bg-primary" style={{ width: `${levelPercent}%` }} /></div>
                          <p className="mt-1 text-[10px] text-muted-foreground">{hasFrames ? t("Input level: {{percent}}%", { percent: formatNumber(Math.round(levelPercent)) }) : t("No sound detected")}</p>
                        </div>;
                      })}
                      <p className="flex items-center gap-1.5 text-[11px] text-muted-foreground"><Volume2 className="h-3.5 w-3.5" />{deviceTestMutation.data.testTonePlayed ? t("Speaker sound played") : t("Speaker sound unavailable")} · {t("Echo reduction")} {deviceTestMutation.data.aecActive ? t("ready") : t("unavailable")}</p>
                    </div>}
                  </div>
                </div>
                </section>
                <aside className="h-fit min-w-0 rounded-[22px] border border-border/70 bg-background/45 p-4 min-[1380px]:sticky min-[1380px]:top-4">
                  <div className="flex items-center gap-2">
                    <ShieldCheck className={`h-4 w-4 ${longSessionReady ? "text-emerald-600 dark:text-emerald-300" : "text-amber-600 dark:text-amber-300"}`} />
                    <h3 className="text-sm font-semibold">{longSessionReady ? t("Ready for a long meeting") : t("Check before a long meeting")}</h3>
                  </div>
                  <p className="mt-1.5 text-xs leading-5 text-muted-foreground">
                    {longSessionReady
                      ? t("Your recording is saved every 30 seconds and can continue for up to 5 hours. The final transcript starts after you stop.")
                      : !capabilitiesQuery.data?.nativeMeetingCapture
                        ? t("Meeting audio recording is not available on this PC right now.")
                        : longSession?.availableFreeBytes == null
                          ? t("Scriber could not check free space. Keep at least 6 GB free before a five-hour meeting.")
                          : !longSession.storageReady
                            ? t("There is not enough free space for a five-hour meeting.")
                            : !finalProviderCapability?.fiveHourSupported
                              ? t("The selected transcription option cannot process a five-hour meeting. Choose another in Settings.")
                              : t("Scriber could not confirm that this setup is ready for five hours.")}
                  </p>
                  <div className="mt-4 divide-y divide-border/60 rounded-xl border border-border/60 bg-muted/20 px-3">
                    <div className="flex items-center justify-between gap-3 py-2.5 text-xs"><span className="text-muted-foreground">{t("Audio recording")}</span><span className={capabilitiesQuery.data?.nativeMeetingCapture ? "text-emerald-600 dark:text-emerald-300" : "text-amber-700 dark:text-amber-300"}>{capabilitiesQuery.data?.nativeMeetingCapture ? t("Ready") : t("Unavailable")}</span></div>
                    <div className="flex items-center justify-between gap-3 py-2.5 text-xs"><span className="text-muted-foreground">{t("Safety saves")}</span><span>{t("Every {{seconds}} s", { seconds: formatNumber(longSession?.checkpointIntervalSeconds ?? 30) })}</span></div>
                    <div className="flex items-center justify-between gap-3 py-2.5 text-xs"><span className="text-muted-foreground">{t("Free storage")}</span><span className={longSession?.storageReady ? "text-emerald-600 dark:text-emerald-300" : "text-amber-700 dark:text-amber-300"}>{longSession?.availableFreeBytes != null ? formatImportBytes(longSession.availableFreeBytes, formatNumber) : t("Not checked")}</span></div>
                    <div className="flex items-center justify-between gap-3 py-2.5 text-xs"><span className="text-muted-foreground">{t("Estimated recording time")}</span><span>{formatCapacity(longSession?.estimatedCaptureSeconds, t, formatNumber)}</span></div>
                    <div className="flex items-center justify-between gap-3 py-2.5 text-xs"><span className="text-muted-foreground">{t("Final transcript")}</span><span className={finalProviderCapability?.fiveHourSupported ? "text-emerald-600 dark:text-emerald-300" : "text-amber-700 dark:text-amber-300"}>{finalProviderDurationLabel}</span></div>
                    <div className="flex items-center justify-between gap-3 py-2.5 text-xs"><span className="text-muted-foreground">{t("Speaker names")}</span><span className={finalProviderCapability?.batchDiarization ? "text-emerald-600 dark:text-emerald-300" : "text-amber-700 dark:text-amber-300"}>{finalProviderCapability?.batchDiarization ? t("Included") : t("Up to 60 min")}</span></div>
                  </div>
                  {!finalProviderCapability?.batchDiarization && <p className="mt-2 text-[11px] leading-4 text-muted-foreground">{t("For meetings over 60 minutes, choose a transcription option that includes speaker names.")}</p>}
                </aside>
              </div>
            </div>
          ) : detailQuery.isLoading ? (
            <div className="space-y-4"><div className="h-12 animate-pulse rounded-xl bg-muted" /><div className="h-96 animate-pulse rounded-2xl bg-muted/70" /></div>
          ) : detailQuery.isError || !detail ? (
            <div className="flex h-full items-center justify-center text-center"><div><AlertTriangle className="mx-auto mb-3 h-8 w-8 text-destructive" /><p className="font-medium">{t("Meeting could not be loaded.")}</p></div></div>
          ) : (
            <div className="flex h-full min-h-0 flex-col">
              <header className="flex flex-col gap-4 border-b border-border/60 px-5 py-5 sm:flex-row sm:items-center sm:justify-between md:px-6 lg:px-7">
                <div className="min-w-0">
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    className="mb-2 -ml-2 h-7 px-2 text-xs min-[1100px]:hidden"
                    onClick={() => setLocation("/meetings")}
                  >
                    <ChevronLeft className="mr-1 h-3.5 w-3.5" />{t("All meetings")}
                  </Button>
                  <div className="flex flex-wrap items-center gap-2">
                    <h2 className="truncate font-heading text-[22px] font-semibold tracking-[-0.025em] md:text-[24px]">{detail.title}</h2>
                    <Badge variant="outline" className={stateTone(detail.state)}>{stateLabel(detail.state, t)}</Badge>
                  </div>
                  <p className="mt-1 text-xs text-muted-foreground">{t("Started {{moment}}", { moment: formatMoment(detail.startedAt || detail.createdAt, formatDate) })}</p>
                </div>
                {(detail.state === "recording" || detail.state === "paused") && <MeetingElapsedTime startedAt={detail.startedAt} audioGaps={detail.audioGaps} paused={detail.state === "paused"} pausedAtTimelineMs={detail.captureMetadata.pauseStartedAtMs} pausedAtUtc={detail.captureMetadata.pauseStartedAtUtc} recordingTimelineOffsetMs={detail.captureMetadata.timelineOffsetMs} recordingTimelineStartedAtUtc={detail.captureMetadata.timelineStartedAtUtc} finalProviderMaxDurationSeconds={detailFinalProviderCapability?.maxDurationSeconds} />}
                <div className="flex flex-wrap gap-2">
                  {["ready", "analysis_failed"].includes(detail.state) && detail.reprocessing?.processingRunning && <Button type="button" variant="outline" className="h-9" disabled>
                    <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />{detail.reprocessing.speakerIdentityRunning ? t("Refreshing speakers…") : t("Processing…")}
                  </Button>}
                  {["ready", "analysis_failed"].includes(detail.state) && detail.reprocessing && !detail.reprocessing.processingRunning && (detail.reprocessing.speakerIdentityAvailable || detail.reprocessing.fullTranscriptAvailable) && <Button type="button" variant="outline" className="h-9 active:scale-[0.97]" onClick={openReprocessDialog}>
                    <RefreshCw className="mr-2 h-3.5 w-3.5" />{t("Process again")}
                  </Button>}
                  {detail.state === "ready" && <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button type="button" variant="outline" className="h-9 active:scale-[0.97]">
                        <Download className="mr-2 h-3.5 w-3.5" />{t("Save or share")}<ChevronDown className="ml-2 h-3.5 w-3.5 text-muted-foreground" />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end" className="min-w-52">
                      {(["json", "md", "pdf", "docx"] as const).map((format) => (
                        <DropdownMenuItem
                          key={format}
                          aria-label={t("Export meeting as {{format}}", { format: format.toUpperCase() })}
                          disabled={exportMutation.isPending}
                          onSelect={() => {
                            exportMutation.mutate({
                              path: `/api/meetings/${detail.id}/export/${format}`,
                              fallbackName: `${detail.title}.${format}`,
                            });
                          }}
                        >
                          <FileText className="mr-2 h-3.5 w-3.5" />{t("Save {{format}}", { format: format === "docx" ? t("Word document") : format === "md" ? "Markdown" : format.toUpperCase() })}
                        </DropdownMenuItem>
                      ))}
                      <DropdownMenuSeparator />
                      <DropdownMenuItem
                        disabled={exportMutation.isPending || !hasPlayableAudio}
                        onSelect={() => exportMutation.mutate({
                          path: `/api/meetings/${detail.id}/export/audio`,
                          fallbackName: `${detail.title} - audio.opus`,
                        })}
                        className="items-start"
                      >
                        <Headphones className="mr-2 mt-0.5 h-3.5 w-3.5 shrink-0" />
                        <span>
                          <span className="block">{t("Save compressed audio")}</span>
                          <span className="mt-0.5 block text-[10px] leading-4 text-muted-foreground">{hasPlayableAudio ? t("Small Opus file for sharing or archiving") : t("Compressed audio is not retained for this meeting")}</span>
                        </span>
                      </DropdownMenuItem>
                      <DropdownMenuSeparator />
                      <DropdownMenuItem
                        onSelect={() => setEmailDialogOpen(true)}
                      >
                        <Mail className="mr-2 h-3.5 w-3.5" />{t("Create email draft")}
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>}
                  {detail.state === "recording" && <Button variant="outline" className="w-32" disabled={controlMutation.isPending} onClick={() => controlMutation.mutate({ id: detail.id, action: "pause" })}><CirclePause className="h-4 w-4" />{t("Pause")}</Button>}
                  {detail.state === "paused" && <Button variant="outline" className="w-32" disabled={controlMutation.isPending} onClick={() => controlMutation.mutate({ id: detail.id, action: "resume" })}><CirclePlay className="h-4 w-4" />{t("Resume")}</Button>}
                  {(detail.state === "recording" || detail.state === "paused") && <Button variant="destructive" className="w-32" disabled={controlMutation.isPending} onClick={() => controlMutation.mutate({ id: detail.id, action: "stop" })}><Square className="h-4 w-4" />{t("Stop")}</Button>}
                  {OPEN_STATES.has(detail.state) && controlMutation.isPending && <Loader2 className="h-5 w-5 animate-spin self-center text-muted-foreground" />}
                </div>
              </header>

              {lastExport && (
                <div className="border-b border-emerald-300/60 bg-emerald-500/10 px-5 py-3 text-emerald-950 dark:text-emerald-100 sm:px-6" role="status">
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                    <div className="flex min-w-0 items-start gap-3">
                      <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600 dark:text-emerald-300" aria-hidden="true" />
                      <div className="min-w-0">
                        <p className="text-sm font-semibold">
                          {lastExport.desktop
                            ? t("Saved in {{folder}}", {
                                folder: meetingExportFolderName(lastExport.directory) || t("the folder you chose"),
                              })
                            : t("Download started")}
                        </p>
                        <p className="mt-0.5 truncate text-xs opacity-80" title={lastExport.desktop ? lastExport.path : lastExport.filename}>
                          {lastExport.desktop ? lastExport.path : t("{{filename}} · Check your browser's Downloads folder", { filename: lastExport.filename })}
                        </p>
                      </div>
                    </div>
                    <div className="flex flex-wrap items-center gap-2 sm:shrink-0">
                      {lastExport.desktop && (
                        <>
                          <Button type="button" size="sm" variant="outline" className="h-8 bg-background/70 active:scale-[0.97]" onClick={() => void runSavedExportAction("open")}>
                            <FileText className="mr-2 h-3.5 w-3.5" />{t("Open file")}
                          </Button>
                          <Button type="button" size="sm" variant="outline" className="h-8 bg-background/70 active:scale-[0.97]" onClick={() => void runSavedExportAction("reveal")}>
                            <FolderOpen className="mr-2 h-3.5 w-3.5" />{t("Open folder")}
                          </Button>
                        </>
                      )}
                      <Button type="button" size="icon" variant="ghost" className="h-8 w-8 active:scale-[0.97]" onClick={() => setLastExport(null)} aria-label={t("Dismiss saved export message")}>
                        <X className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                  </div>
                </div>
              )}

              {(detail.state === "recording" || detail.state === "paused") && (
                <div className="border-b border-border/60 bg-muted/20 px-5 py-3 sm:px-6">
                  <div className="grid gap-2 sm:grid-cols-2">
                    {(["microphone", "system"] as const).map((source) => (
                      <MeetingLevelMeter key={source} source={source} paused={detail.state === "paused"} levels={audioLevelsRef} />
                    ))}
                  </div>
                  <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-muted-foreground" role="status">
                    <ShieldCheck className="h-3.5 w-3.5 text-emerald-600 dark:text-emerald-300" />
                    <MeetingCheckpointStatus checkpoint={latestCheckpoint} expectedTrackCount={expectedCheckpointTrackCount} paused={detail.state === "paused"} />
                    {latestCheckpoint && <span aria-hidden="true">·</span>}
                    {latestCheckpoint && <span>{detail.transcriptionMode === "live_final"
                      ? t(latestCheckpoint.segmentCount === 1 ? "{{count}} transcript part saved" : "{{count}} transcript parts saved", { count: formatNumber(latestCheckpoint.segmentCount) })
                      : t("Transcript starts after you stop")}</span>}
                  </div>
                  <div className="mt-2 space-y-2">{(["microphone", "system"] as const).map((source) => liveStatuses[source] && (
                    <div key={`${source}-${liveStatuses[source]!.status}`} role="status" className={`rounded-lg border px-3 py-2 text-xs ${liveStatuses[source]!.status === "recovered" ? "border-emerald-300/60 bg-emerald-500/10 text-emerald-900 dark:text-emerald-100" : "border-amber-300/60 bg-amber-500/10 text-amber-900 dark:text-amber-100"}`}>
                      {source === "microphone" ? t("Microphone") : t("System audio")}: {liveStatuses[source]!.status === "reconnecting" ? t("live text is temporarily offline and reconnecting. Audio recording continues safely.") : liveStatuses[source]!.status === "degraded" ? t("live text may miss words for a moment. Audio recording continues safely.") : t("live text is back. The final transcript will be created from saved audio.")}{liveStatuses[source]!.reconnectCount > 0 ? ` ${t("Attempt {{count}}.", { count: formatNumber(liveStatuses[source]!.reconnectCount) })}` : ""}
                    </div>
                  ))}</div>
                </div>
              )}

              {(["stopping", "finalizing", "analyzing"] as MeetingState[]).includes(detail.state) && <div className="mt-4 rounded-xl border border-border/60 bg-muted/35 px-4 py-3" role="status">
                <div className="flex items-center justify-between gap-3 text-xs"><span className="font-medium">{detail.state === "analyzing" ? t("Creating summary, decisions, and action items") : detail.state === "stopping" ? t("Finishing the recording safely") : t("Creating the final transcript from saved audio")}</span><span className="tabular-nums text-muted-foreground">{meetingProgress ? `${formatNumber(Math.round(meetingProgress.progress * 100))}%` : t("Processing…")}</span></div>
                <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-muted" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={meetingProgress ? Math.round(meetingProgress.progress * 100) : undefined} aria-valuetext={meetingProgress ? undefined : t("Processing…")}>
                  {meetingProgress
                    ? <div className="h-full origin-left rounded-full bg-primary transition-transform duration-200 motion-reduce:transition-none" style={{ transform: `scaleX(${meetingProgress.progress})` }} />
                    : <div className="h-full w-1/3 animate-pulse rounded-full bg-primary/65 motion-reduce:animate-none" />}
                </div>
              </div>}

              {detail.errorMessage && (
                <div className="mt-4 rounded-xl border border-amber-300/50 bg-amber-500/10 px-4 py-3 text-sm text-amber-900 dark:text-amber-100">
                  <p>{localizeMeetingErrorMessage(detail.errorMessage, t)}</p>
                  {detail.state === "finalization_failed" && (
                    <label className="mt-3 block max-w-sm text-xs font-semibold">
                      {t("Try another transcription option")}
                      <select
                        value={retryFinalProvider}
                        onChange={(event) => setRetryFinalProvider(event.target.value)}
                        className="mt-1.5 h-9 w-full rounded-lg border border-border bg-background px-3 text-sm font-normal text-foreground outline-none focus:ring-2 focus:ring-primary"
                      >
                        {(profilesQuery.data?.finalProviderOptions ?? []).map((option) => (
                          <option key={option.id} value={option.id} disabled={option.available === false}>
                            {option.label} · {option.model}{option.available === false ? ` · ${t("not configured")}` : ""}
                          </option>
                        ))}
                      </select>
                      <span className="mt-1 block font-normal opacity-80">
                        {t("Choose another available option if this recording is longer than the previous one supports.")}
                      </span>
                    </label>
                  )}
                  {["capture_failed", "finalization_failed", "analysis_failed", "interrupted"].includes(detail.state) && (
                    <div className="mt-3 flex flex-wrap gap-2">
                      {detail.state === "interrupted" && (
                        <Button type="button" size="sm" disabled={controlMutation.isPending} onClick={() => controlMutation.mutate({ id: detail.id, action: "resume" })}>
                          <CirclePlay className="mr-2 h-3.5 w-3.5" />{t("Resume capture")}
                        </Button>
                      )}
                      <Button
                        type="button"
                        size="sm"
                        variant="outline"
                        disabled={recoveryMutation.isPending || (detail.state === "finalization_failed" && !retryFinalProvider)}
                        onClick={() => recoveryMutation.mutate({
                          id: detail.id,
                          action: "retry",
                          finalProvider: detail.state === "finalization_failed" ? retryFinalProvider : undefined,
                        })}
                      >
                        {recoveryMutation.isPending && <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />}
                        {detail.state === "analysis_failed" ? t("Try meeting brief again") : t("Create transcript from saved audio")}
                      </Button>
                      <Button type="button" size="sm" variant="ghost" disabled={recoveryMutation.isPending} onClick={() => recoveryMutation.mutate({ id: detail.id, action: "discard" })}>{t("Discard")}</Button>
                    </div>
                  )}
                </div>
              )}

              {outputsStale && (
                <div className="mx-5 mt-3 flex flex-col gap-3 rounded-xl border border-amber-300/60 bg-amber-500/10 px-4 py-3 text-sm text-amber-950 dark:text-amber-100 sm:mx-6 sm:flex-row sm:items-center sm:justify-between" role="status">
                  <div className="min-w-0">
                    <p className="font-semibold">{t("Transcript corrected after this brief was generated")}</p>
                    <p className="mt-0.5 text-xs opacity-80">{t("Playback, search, and new exports use the correction. Regenerate the brief when its wording depends on the edited passage.")}</p>
                  </div>
                  {detail.state === "ready" && (
                    <Button type="button" size="sm" variant="outline" className="shrink-0 active:scale-[0.97]" disabled={analysisMutation.isPending || !hasCanonicalTranscript} onClick={generateAnalysis}>
                      {analysisMutation.isPending && <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />}{t("Regenerate brief")}
                    </Button>
                  )}
                </div>
              )}

              {!OPEN_STATES.has(detail.state) && (
                <SpeakerAttendeeAssignments
                  meetingId={detail.id}
                  calendarEvent={detail.captureMetadata.calendarEvent ?? null}
                  playableSpeakerIds={playableSpeakerIds}
                  onPlaySpeaker={playSpeakerSnippet}
                  focusRequest={speakerAssignmentRequest}
                  onAssignmentsChanged={() => {
                    void queryClient.invalidateQueries({
                      queryKey: ["/api/meetings", detail.id],
                      exact: true,
                    });
                  }}
                />
              )}

              <MeetingWorkspaceTabs value={workspaceView} onChange={setWorkspaceView} />
              {detail.segments.length > 0 && hasPlayableAudio && (
                <div className="mx-5 mt-3 flex flex-col gap-2 rounded-xl bg-muted/45 px-3 py-2 sm:mx-6 sm:flex-row sm:flex-wrap sm:items-center">
                  <audio
                    ref={audioRef}
                    controls
                    preload="metadata"
                    src={apiUrl(`/api/meetings/${detail.id}/audio`)}
                    onLoadedMetadata={handlePlaybackMetadata}
                    onTimeUpdate={handlePlaybackTimeUpdate}
                    onPlay={() => setPlaybackError("")}
                    onError={() => setPlaybackError("The saved meeting audio could not be loaded.")}
                    className="h-8 w-full sm:ml-auto sm:max-w-md"
                  />
                  {playbackError && <p className="text-xs text-destructive sm:basis-full" role="alert">{t(playbackError)}</p>}
                </div>
              )}
              {detail.segments.length > 0 && !hasPlayableAudio && (
                <div className="mx-5 mt-3 flex items-center gap-2 rounded-xl border border-border/60 bg-muted/35 px-3 py-2 text-xs text-muted-foreground sm:mx-6" role="status">
                  <Headphones className="h-3.5 w-3.5" />
                  {detail.captureMetadata.audioPurgedAt
                    ? t("Audio is no longer retained. The transcript and meeting outputs remain available.")
                    : t("No playable audio asset is available for this meeting.")}
                </div>
              )}

              <div className="grid min-h-0 flex-1 2xl:grid-cols-[minmax(0,1fr)_300px]">
                <section className="min-w-0 px-5 py-5 sm:px-6">
                  {workspaceView !== "transcript" ? (
                    <div className="max-w-3xl">
                      {workspaceView === "chat" ? (
                        <div className="space-y-4">
                          <div><h3 className="text-sm font-semibold">{t("Ask this meeting")}</h3><p className="mt-1 text-xs text-muted-foreground">{t("Answers use only this meeting's final transcript. Click a source marker to jump to that moment.")}</p></div>
                          {chatAnswer && <div className="rounded-2xl bg-muted/55 p-4"><p className="whitespace-pre-wrap text-sm leading-7">{chatAnswer.content}</p>{chatAnswer.citations.length > 0 && <div className="mt-3 flex flex-wrap gap-1.5">{chatAnswer.citations.map((citation) => <button type="button" key={citation} onClick={() => seekCitation(citation)}><Badge variant="outline" className="font-mono text-[10px] hover:border-primary">{citation.slice(0, 8)}</Badge></button>)}</div>}</div>}
                          <Textarea value={chatQuestion} onChange={(event) => setChatQuestion(event.target.value)} placeholder={t("What did we decide about the launch?")} rows={3} />
                          <Button disabled={!chatQuestion.trim() || chatMutation.isPending} onClick={() => chatMutation.mutate({ id: detail.id, question: chatQuestion })}>{chatMutation.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}{t("Ask meeting")}</Button>
                        </div>
                      ) : workspaceView === "notes" ? (
                        <div className="max-w-2xl"><h3 className="text-sm font-semibold">{t("Meeting notes")}</h3><p className="mt-1 text-xs text-muted-foreground">{t("Your notes remain separate from generated outputs.")}</p><Textarea value={note} onChange={(event) => setNote(event.target.value)} placeholder={t("Capture decisions and follow-ups...")} rows={8} className="mt-4" />{detail.notes.filter((savedNote) => savedNote.id !== "workspace").map((savedNote) => <div key={savedNote.id} className="mt-3 rounded-xl bg-muted/55 p-3"><p className="text-xs font-medium text-primary">{formatOffset(savedNote.atMs)}</p><p className="mt-1 text-sm leading-6">{savedNote.body}</p></div>)}</div>
                      ) : !analysis ? (
                        <div className="flex min-h-64 items-center justify-center rounded-2xl border border-dashed border-border text-center text-sm text-muted-foreground">
                          <div>{detail.state === "analyzing" ? <Loader2 className="mx-auto mb-3 h-6 w-6 animate-spin" /> : <AlertTriangle className="mx-auto mb-3 h-6 w-6" />}<p>{detail.state === "analyzing" ? t("Creating your meeting brief…") : t("No meeting brief yet.")}</p>{detail.state === "ready" && <Button type="button" size="sm" variant="outline" className="mt-4" disabled={analysisMutation.isPending || !hasCanonicalTranscript} onClick={generateAnalysis}>{analysisMutation.isPending && <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />}{t("Create meeting brief")}</Button>}</div>
                        </div>
                      ) : workspaceView === "overview" ? (
                        <div className="max-w-4xl">
                          <div className="flex items-center justify-between gap-3">
                            <div><p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-primary">{t("Meeting brief")}</p><h3 className="mt-1 text-lg font-semibold tracking-tight">{t("What matters now")}</h3></div>
                            {detail.state === "ready" && <Button type="button" size="sm" variant="outline" disabled={analysisMutation.isPending || !hasCanonicalTranscript} onClick={generateAnalysis}>{analysisMutation.isPending && <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />}{t("Regenerate")}</Button>}
                          </div>
                          <section className="mt-5 border-l-2 border-primary pl-4">
                            <div className="flex items-center gap-2"><Sparkles className="h-4 w-4 text-primary" /><h4 className="text-sm font-semibold">{t("Key outcome")}</h4></div>
                            <p className="mt-2 whitespace-pre-wrap text-sm leading-7 text-foreground/90">{analysis.executiveSummary ? String(analysis.executiveSummary) : t("No summary was produced.")}</p>
                          </section>
                          <div className="mt-7 grid gap-7">
                            <section><div className="flex items-center gap-2"><Check className="h-4 w-4 text-primary" /><h4 className="text-sm font-semibold">{t("Decisions")}</h4></div><EvidenceList items={analysis.decisions} onCitation={seekCitation} /></section>
                            <section><div className="flex items-center gap-2"><AlertTriangle className="h-4 w-4 text-primary" /><h4 className="text-sm font-semibold">{t("Risks and open questions")}</h4></div><EvidenceList items={[...(Array.isArray(analysis.risks) ? analysis.risks : []), ...(Array.isArray(analysis.openQuestions) ? analysis.openQuestions : [])]} onCitation={seekCitation} /></section>
                          </div>
                          <section className="mt-7 border-t border-border/60 pt-6"><div className="flex items-center gap-2"><FileText className="h-4 w-4 text-primary" /><h4 className="text-sm font-semibold">{t("Action items")}</h4></div><ActionItems items={detail.actionItems ?? []} saving={actionItemMutation.isPending} onCitation={seekCitation} onChange={(item, changes) => actionItemMutation.mutate({ id: detail.id, itemId: item.id, changes })} /></section>
                        </div>
                      ) : workspaceView === "decisions" ? <EvidenceList items={analysis.decisions} onCitation={seekCitation} />
                        : workspaceView === "actions" ? <ActionItems items={detail.actionItems ?? []} saving={actionItemMutation.isPending} onCitation={seekCitation} onChange={(item, changes) => actionItemMutation.mutate({ id: detail.id, itemId: item.id, changes })} />
                          : <EvidenceList items={analysis.openQuestions} onCitation={seekCitation} />}
                    </div>
                  ) : <>
                  <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
                    <div className="flex items-center gap-2"><Users className="h-4 w-4 text-muted-foreground" /><h3 className="text-sm font-semibold">{t("Transcript")}</h3></div>
                    <span className="text-xs text-muted-foreground">{transcriptSearch.trim() ? t("{{visible}} of {{total}} parts", { visible: formatNumber(visibleTranscriptSegments.length), total: formatNumber(groupedSegments.length) }) : t(groupedSegments.length === 1 ? "{{count}} part" : "{{count}} parts", { count: formatNumber(groupedSegments.length) })}</span>
                  </div>
                  <label className="relative mb-4 block max-w-xl">
                    <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
                    <Input
                      type="search"
                      value={transcriptSearch}
                      onChange={(event) => setTranscriptSearch(event.target.value)}
                      placeholder={t("Search transcript, speaker, or phrase")}
                      className="h-9 pl-9 text-sm"
                      aria-label={t("Search this meeting transcript")}
                    />
                  </label>
                  {detail.speakers.length > 0 && <div className="mb-4 grid gap-2 lg:grid-cols-2">
                    {detail.speakers.map((speaker) => {
                      const speakerProfileId = speaker.profileId;
                      const profile = speakerProfileId ? speakerProfilesById.get(speakerProfileId) : null;
                      const savedVoiceName = profile?.displayName || "";
                      const savedVoicePreviewUrl = profile?.preview?.url || "";
                      const rawMeetingSpeakerName = speaker.displayName || speaker.label;
                      const meetingSpeakerName = rawMeetingSpeakerName === "Meeting audio"
                        ? t("Meeting audio")
                        : rawMeetingSpeakerName;
                      const voiceEvidenceCount = speaker.voiceMatch?.evidenceCount ?? 0;
                      const hasSpeakerSnippet = canPlaySpeakerSamples && liveSegments.some((segment) => (
                        segment.speakerId === speaker.id && segment.endMs > segment.startMs
                      ));
                      const renamingProfile = speakerProfileNameMutation.isPending
                        && speakerProfileNameMutation.variables?.profileId === speaker.profileId;
                      return <div key={speaker.id} className="rounded-xl border border-border/70 bg-muted/25 px-3 py-2.5 text-xs">
                        <div className="flex min-w-0 flex-wrap items-center gap-2">
                          <span className="shrink-0 text-muted-foreground">{speaker.sourceHint === "microphone" ? t("Mic") : t("Remote")}</span>
                          <input
                            key={`${speaker.id}-${speaker.updatedAt}`}
                            defaultValue={meetingSpeakerName}
                            aria-label={t("Name in this meeting for {{speaker}}", { speaker: meetingSpeakerName })}
                            data-testid={`meeting-speaker-name-${speaker.id}`}
                            className="min-w-24 flex-1 bg-transparent font-medium outline-none focus-visible:rounded focus-visible:ring-2 focus-visible:ring-ring"
                            onBlur={(event) => {
                              const displayName = event.target.value.trim();
                              if (displayName && displayName !== meetingSpeakerName) {
                                speakerMutation.mutate({ id: detail.id, speakerId: speaker.id, displayName });
                              }
                            }}
                          />
                          <button
                            type="button"
                            disabled={!hasSpeakerSnippet}
                            onClick={() => playSpeakerSnippet(speaker.id)}
                            className="inline-flex h-7 shrink-0 items-center gap-1.5 rounded-md px-2 text-[11px] font-medium text-muted-foreground hover:bg-background hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
                            title={hasSpeakerSnippet ? t("Play a 5 to 8 second sample from this speaker") : t("No saved audio sample is available")}
                          >
                            <CirclePlay className="h-3.5 w-3.5" />{t("Meeting sample")}
                          </button>
                        </div>
                        {speakerProfileId && <div className="mt-2 flex min-w-0 flex-wrap items-center gap-2 border-t border-border/55 pt-2">
                          <ShieldCheck className="h-3.5 w-3.5 shrink-0 text-primary" />
                          <span className="shrink-0 text-[11px] text-muted-foreground">{t("Saved voice")}</span>
                          <input
                            key={`${speakerProfileId}-${profile?.updatedAt || "meeting"}`}
                            defaultValue={savedVoiceName}
                            placeholder={speakerProfilesQuery.isLoading ? t("Loading name…") : t("Saved voice unavailable")}
                            disabled={!profile || renamingProfile}
                            aria-label={t("Saved voice profile name for {{speaker}}", { speaker: savedVoiceName || meetingSpeakerName })}
                            className="min-w-24 flex-1 rounded-md border border-transparent bg-background/65 px-2 py-1 font-medium outline-none hover:border-border focus:border-primary focus:ring-2 focus:ring-primary/15 disabled:opacity-60"
                            onBlur={(event) => {
                              const displayName = event.target.value.trim();
                              if (profile && displayName && displayName !== savedVoiceName) {
                                speakerProfileNameMutation.mutate({ profileId: speakerProfileId, displayName });
                              }
                            }}
                          />
                          {renamingProfile && <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />}
                          {profile && savedVoicePreviewUrl && <button
                            type="button"
                            onClick={() => void playSavedVoicePreview(profile.id, savedVoicePreviewUrl)}
                            className="inline-flex h-7 shrink-0 items-center gap-1.5 rounded-md px-2 text-[10px] font-medium text-muted-foreground hover:bg-background hover:text-foreground"
                            title={t("Play the short sample stored for this saved voice")}
                          >
                            <CirclePlay className="h-3.5 w-3.5" />{t("Saved sample")}
                          </button>}
                          {voiceEvidenceCount > 0 && <span className="text-[10px] text-muted-foreground">
                            {t(voiceEvidenceCount === 1 ? "{{count}} matching sample" : "{{count}} matching samples", { count: formatNumber(voiceEvidenceCount) })}
                          </span>}
                          <span className="text-[10px] text-muted-foreground">{profile ? t("Updates Settings too") : speakerProfilesQuery.isLoading ? t("Loading saved profile") : t("Profile unavailable")}</span>
                          <button
                            type="button"
                            className="rounded px-1.5 py-1 text-[10px] text-muted-foreground hover:bg-background hover:text-foreground"
                            disabled={splitSpeakerMutation.isPending}
                            onClick={() => splitSpeakerMutation.mutate({ id: detail.id, speakerId: speaker.id })}
                            title={t("Do not match this speaker to the saved voice")}
                          >
                            {t("Wrong match")}
                          </button>
                        </div>}
                      </div>;
                    })}
                  </div>}
                  <div>
                    {groupedSegments.length === 0 ? (
                      <div className="flex min-h-64 items-center justify-center rounded-2xl border border-dashed border-border text-center text-sm text-muted-foreground">
                        <div><Waves className="mx-auto mb-3 h-7 w-7" /><p>{OPEN_STATES.has(detail.state) ? detail.transcriptionMode === "final_only" ? t("Recording safely. The transcript appears after you stop.") : t("Listening for speech…") : t("No transcript is available.")}</p></div>
                      </div>
                    ) : visibleTranscriptSegments.length === 0 ? (
                      <div className="flex min-h-48 items-center justify-center rounded-2xl border border-dashed border-border text-center text-sm text-muted-foreground">
                        <div><Search className="mx-auto mb-3 h-6 w-6" /><p>{t("No transcript text matches “{{query}}”.", { query: transcriptSearch.trim() })}</p></div>
                      </div>
                    ) : <VirtualMeetingTranscript
                      key={detail.id}
                      segments={visibleTranscriptSegments}
                      search={transcriptSearch}
                      hasPlayableAudio={hasPlayableAudio}
                      isLive={detail.state === "recording" || detail.state === "paused"}
                      onPlay={playSegment}
                      canAssignSpeakers={!OPEN_STATES.has(detail.state)}
                      onAssignSpeaker={focusSpeakerAssignment}
                      canEdit={detail.state === "ready" && visibleTranscriptSegments.every((segment) => segment.revision === "canonical")}
                      savingSegmentId={segmentEditMutation.isPending ? segmentEditMutation.variables?.segment.id ?? "" : ""}
                      onSave={(segment, text) => segmentEditMutation.mutate({ segment, action: "edit", text })}
                      onUndo={(segment) => segmentEditMutation.mutate({ segment, action: "undo" })}
                    />}
                  </div>
                  </>}
                </section>

                <aside className="border-t border-border/60 bg-muted/15 px-5 py-5 2xl:border-l 2xl:border-t-0">
                  <div className="flex items-center gap-2"><NotebookPen className="h-4 w-4 text-muted-foreground" /><h3 className="text-sm font-semibold">{t("Live notes")}</h3></div>
                  <div className="mt-3 space-y-2">
                    <Textarea value={note} onChange={(event) => { const body = event.target.value; setNote(body); noteDraftRef.current = { meetingId: selectedId, body: body.trim(), savedBody: noteDraftRef.current.savedBody }; }} placeholder={t("Capture decisions and follow-ups…")} rows={5} />
                    <p className="flex items-center text-xs text-muted-foreground">
                      {noteMutation.isPending ? <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" /> : <Check className="mr-2 h-3.5 w-3.5" />}
                      {noteMutation.isPending ? t("Saving…") : t("Notes autosave and AI regeneration never overwrites them.")}
                    </p>
                  </div>
                  <div className="mt-5 space-y-2">
                    {detail.notes.length === 0 && <p className="text-xs leading-5 text-muted-foreground">{t("Notes are timestamped and retained with this meeting.")}</p>}
                    {detail.notes.filter((savedNote) => savedNote.id !== "workspace").map((savedNote) => <div key={savedNote.id} className="rounded-xl bg-muted/60 px-3 py-2.5"><p className="text-xs font-medium text-primary">{formatOffset(savedNote.atMs)}</p><p className="mt-1 text-sm leading-5">{savedNote.body}</p></div>)}
                  </div>
                  <details className="group mt-5 rounded-xl border border-border/60 bg-background/35">
                    <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-3 py-2.5 text-xs font-semibold marker:content-none">
                      <span>{t("Technical details")}</span>
                      <ChevronDown className="h-3.5 w-3.5 text-muted-foreground group-open:rotate-180 motion-reduce:transform-none" />
                    </summary>
                    <div className="border-t border-border/60 p-3">
                      <div className="flex items-center gap-2"><Sparkles className="h-4 w-4 text-primary" /><h3 className="text-xs font-semibold">{t("Models used")}</h3></div>
                      <dl className="mt-3 space-y-2 text-[11px]">
                        <div className="flex items-start justify-between gap-3"><dt className="text-muted-foreground">{t("Live transcript")}</dt><dd className="text-right font-mono">{detail.origin === "imported" ? t("Not used (imported)") : detail.transcriptionMode === "final_only" ? t("Not used (transcript after meeting)") : detailLiveModel || detail.liveProvider}</dd></div>
                        <div className="flex items-start justify-between gap-3"><dt className="text-muted-foreground">{t("Final transcript")}</dt><dd className="text-right font-mono">{detail.finalRoute?.model || detail.finalProvider}</dd></div>
                        <div className="flex items-start justify-between gap-3"><dt className="text-muted-foreground">{t("Summary and actions")}</dt><dd className="break-all text-right font-mono">{technicalAnalysisModel}</dd></div>
                        {([[
                          "Speaker separation",
                          detail.processingComponents?.diarization,
                        ], [
                          "Voice activity detection",
                          detail.processingComponents?.vad,
                        ], [
                          "Turn detection",
                          detail.processingComponents?.turnDetection,
                        ]] as const).map(([label, component]) => <div key={label} className="flex items-start justify-between gap-3 border-t border-border/45 pt-2">
                          <dt className="text-muted-foreground">{t(label)}</dt>
                          <dd className="max-w-[58%] text-right">
                            <span className="block font-mono">{processingComponentLabel(component, t)}</span>
                            {processingComponentModeLabel(component, t, formatNumber) && <span className="mt-0.5 block text-[10px] text-muted-foreground">{processingComponentModeLabel(component, t, formatNumber)}</span>}
                          </dd>
                        </div>)}
                      </dl>
                      {detail.state === "ready" && aecMetrics && <div className="mt-4 border-t border-border/60 pt-3">
                        <div className="flex items-center justify-between gap-3">
                          <span className="text-xs font-medium">{t("Render-active attenuation")}</span>
                          <span className={`font-mono text-sm font-semibold tabular-nums ${typeof aecMetrics.echoReductionDb === "number" && aecMetrics.echoReductionDb > 0 ? "text-emerald-600 dark:text-emerald-300" : "text-muted-foreground"}`}>
                            {typeof aecMetrics.echoReductionDb === "number" ? `${formatNumber(aecMetrics.echoReductionDb, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} dB` : t("Not measured")}
                          </span>
                        </div>
                        <p className="mt-1 text-[11px] leading-4 text-muted-foreground">{t("Raw-to-clean microphone energy while system audio was active · {{seconds}}s measured.", { seconds: formatNumber(Math.round((aecMetrics.renderActiveDurationMs ?? 0) / 1000)) })}</p>
                      </div>}
                    </div>
                  </details>
                  {detail.state === "ready" && <details className="group mt-6 border-t border-border/60 pt-3">
                    <summary className="flex cursor-pointer list-none items-center justify-between gap-3 py-2 text-sm font-semibold marker:content-none">
                      <span>{t("Delivery & integrations")}</span>
                      <ChevronDown className="h-4 w-4 text-muted-foreground group-open:rotate-180 motion-reduce:transform-none" />
                    </summary>
                    <div className="pt-2">
                    <p className="text-xs leading-5 text-muted-foreground">{t("HTTPS only. Redirects are blocked, and the signing secret is never stored.")}</p>
                    <div className="mt-3 space-y-2">
                      <div><label htmlFor="meeting-webhook-url" className="text-xs text-muted-foreground">{t("Destination URL")}</label><Input id="meeting-webhook-url" value={webhookUrl} onChange={(event) => { setWebhookUrl(event.target.value); setWebhookPreview(null); setWebhookConfirmed(false); }} placeholder="https://automation.example/meeting" className="mt-1 h-9 text-xs" /></div>
                      <div><label htmlFor="meeting-webhook-secret" className="text-xs text-muted-foreground">{t("Optional HMAC secret")}</label><Input id="meeting-webhook-secret" type="password" autoComplete="off" value={webhookSecret} onChange={(event) => setWebhookSecret(event.target.value)} className="mt-1 h-9 text-xs" /></div>
                      <Button type="button" size="sm" variant="outline" disabled={!webhookUrl.trim() || webhookPreviewMutation.isPending} onClick={() => webhookPreviewMutation.mutate({ id: detail.id, url: webhookUrl.trim() })}>{webhookPreviewMutation.isPending && <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />}{t("Preview payload")}</Button>
                    </div>
                    {webhookPreview && <div className="mt-3 rounded-xl border border-border/70 bg-muted/35 p-3 text-xs">
                      <p className="font-medium">{webhookPreview.payload.event || "meeting.ready"}</p>
                      <p className="mt-1 break-all text-muted-foreground">{webhookPreview.target}</p>
                      <dl className="mt-3 grid grid-cols-3 gap-2"><div><dt className="text-muted-foreground">{t("Size")}</dt><dd className="mt-0.5 font-medium">{formatNumber(webhookPreview.byteSize)} B</dd></div><div><dt className="text-muted-foreground">{t("Segments")}</dt><dd className="mt-0.5 font-medium">{formatNumber(webhookPreview.payload.segments?.length ?? 0)}</dd></div><div><dt className="text-muted-foreground">{t("Notes")}</dt><dd className="mt-0.5 font-medium">{formatNumber(webhookPreview.payload.notes?.length ?? 0)}</dd></div></dl>
                      <label className="mt-3 flex cursor-pointer items-start gap-2"><input type="checkbox" checked={webhookConfirmed} onChange={(event) => setWebhookConfirmed(event.target.checked)} className="mt-0.5 h-4 w-4 accent-primary" /><span>{t("I reviewed this target and payload.")}</span></label>
                      <Button type="button" size="sm" className="mt-3" disabled={!webhookConfirmed || !webhookPreview || webhookDeliveryMutation.isPending} onClick={() => webhookPreview && webhookDeliveryMutation.mutate({ id: detail.id, url: webhookUrl, secret: webhookSecret, previewHash: webhookPreview.previewHash, confirmed: webhookConfirmed })}>{webhookDeliveryMutation.isPending && <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />}{t("Send webhook")}</Button>
                    </div>}
                    {(deliveriesQuery.data?.items.length ?? 0) > 0 && <div className="mt-3 space-y-2">{deliveriesQuery.data?.items.slice(0, 3).map((delivery) => <div key={delivery.id} className="rounded-lg bg-muted/45 px-3 py-2 text-xs"><div className="flex items-center justify-between gap-2"><span className="truncate text-muted-foreground">{delivery.target}</span><Badge variant="outline">{t(delivery.status)}</Badge></div><p className="mt-1 text-muted-foreground">{t(delivery.attemptCount === 1 ? "{{count}} attempt" : "{{count}} attempts", { count: formatNumber(delivery.attemptCount) })}</p></div>)}</div>}
                    </div>
                  </details>}
                </aside>
              </div>
            </div>
          )}
        </main>
      </div>
      <Dialog
        open={Boolean(meetingImportCandidate)}
        onOpenChange={(open) => {
          if (!open && !meetingImportBusy) setMeetingImportCandidate(null);
        }}
      >
        <DialogContent className="sm:max-w-[560px]">
          <DialogHeader>
            <DialogTitle>{t("Import a meeting recording")}</DialogTitle>
            <DialogDescription>
              {t("Create a meeting workspace with a transcript, speaker names, summary, search, and linked playback.")}
            </DialogDescription>
          </DialogHeader>
          {meetingImportCandidate && <div className="space-y-4">
            <div className="rounded-xl border border-border/65 bg-muted/30 p-4">
              <div className="flex min-w-0 items-start gap-3">
                <div className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-primary/10 text-primary"><FileUp className="h-4 w-4" /></div>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium">{meetingImportCandidate.file.name}</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {formatImportDuration(meetingImportCandidate.durationSeconds, t)} · {formatImportBytes(meetingImportCandidate.file.size, formatNumber)}
                  </p>
                </div>
              </div>
            </div>
            <div>
              <label htmlFor="meeting-import-title" className="text-xs font-medium text-muted-foreground">{t("Meeting title")}</label>
              <Input
                id="meeting-import-title"
                value={meetingImportCandidate.title}
                disabled={meetingImportBusy}
                onChange={(event) => setMeetingImportCandidate((current) => current ? { ...current, title: event.target.value } : current)}
                className="mt-1.5 h-10"
              />
            </div>
            <div>
              <div className="flex items-center justify-between gap-3">
                <p className="text-xs font-medium text-muted-foreground">{t("Final transcript setting")}</p>
                <Button type="button" size="sm" variant="ghost" className="h-7 px-2 text-[11px]" disabled={meetingImportBusy} onClick={openMeetingSettings}>{t("Change in Settings")}</Button>
              </div>
              {meetingImportProfile && <div className="mt-1.5 rounded-lg border border-border/55 px-3 py-2.5 text-xs leading-5">
                <div className="flex items-center justify-between gap-3"><span className="text-muted-foreground">{t("Transcript service")}</span><span className="text-right font-medium">{meetingImportProfile.stages.find((stage) => stage.id === "final")?.provider || meetingImportProfile.finalProvider} · {meetingImportProfile.stages.find((stage) => stage.id === "final")?.model || meetingImportProfile.finalProvider}</span></div>
                <div className="flex items-center justify-between gap-3"><span className="text-muted-foreground">{t("Maximum duration")}</span><span className="font-medium">{meetingImportFinalProviderCapability?.maxDurationSeconds != null ? formatImportDuration(meetingImportFinalProviderCapability.maxDurationSeconds, t) : t("No published duration limit")}</span></div>
                <div className="flex items-center justify-between gap-3"><span className="text-muted-foreground">{t("Speaker names")}</span><span className="text-right font-medium">{profilesQuery.data?.providerCapabilities[meetingImportProfile.finalProvider]?.batchDiarization ? t("Included") : t("Added on this device · up to 60 min")}</span></div>
                <div className="flex items-center justify-between gap-3"><span className="text-muted-foreground">{t("Language")}</span><span className="font-medium">{meetingImportProfile.language && meetingImportProfile.language !== "auto" ? meetingImportProfile.language : t("Auto")}</span></div>
                <div className="flex items-center justify-between gap-3"><span className="text-muted-foreground">{t("Estimated STT cost")}</span><span className="font-mono font-medium">{meetingImportFinalCostPerAudioHour != null ? t("~${{cost}} / audio hour", { cost: formatNumber(meetingImportFinalCostPerAudioHour, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) }) : t("Provider rate varies")}</span></div>
              </div>}
              {meetingImportExceedsProviderDuration && <div className="mt-2 rounded-lg border border-amber-300/60 bg-amber-500/10 px-3 py-2.5 text-xs leading-5 text-amber-900 dark:text-amber-100" role="alert">
                {t("This transcription option cannot process a recording this long. Choose another option in Meeting settings.")}
              </div>}
            </div>
            {meetingImportBusy && <div className="rounded-xl border border-border/65 bg-muted/25 px-4 py-3" role="status">
              <div className="flex items-center justify-between gap-3 text-xs"><span className="font-medium">{t(meetingImportProgress.stage)}</span><span className="tabular-nums text-muted-foreground">{formatNumber(meetingImportProgress.percentage)}%</span></div>
              <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-muted" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={meetingImportProgress.percentage}><div className="h-full origin-left rounded-full bg-primary transition-transform duration-200 motion-reduce:transition-none" style={{ transform: `scaleX(${meetingImportProgress.percentage / 100})` }} /></div>
              <p className="mt-2 text-xs text-muted-foreground">{t("Scriber first saves a safe local copy. After the upload finishes, you can close this window without losing the import.")}</p>
            </div>}
          </div>}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={meetingImportBusy && (!meetingImportId || meetingImportCancelMutation.isPending)}
              onClick={() => {
                if (meetingImportBusy && meetingImportId) {
                  meetingImportCancelMutation.mutate(meetingImportId);
                  setMeetingImportProgress((current) => mergeMeetingImportProgress(current, {
                    importId: meetingImportId || current.importId,
                    phase: "cancel_requested",
                    stage: "Cancel requested",
                    percentage: 0,
                  }));
                } else setMeetingImportCandidate(null);
              }}
            >
              {meetingImportBusy ? t("Cancel import") : t("Cancel")}
            </Button>
            <Button
              type="button"
              disabled={meetingImportBusy || !meetingImportCandidate?.title.trim() || !meetingImportProfile?.available || meetingImportExceedsProviderDuration}
              onClick={() => {
                if (!meetingImportCandidate || !meetingImportProfile) return;
                meetingImportMutation.mutate({
                  file: meetingImportCandidate.file,
                  title: meetingImportCandidate.title,
                  profile: meetingImportProfile,
                });
              }}
            >
              {meetingImportBusy && <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />}
              {meetingImportBusy ? t("Importing…") : t("Import recording")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={emailDialogOpen} onOpenChange={setEmailDialogOpen}>
        <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-[680px]">
          <DialogHeader>
            <DialogTitle>{t("Share meeting by email")}</DialogTitle>
            <DialogDescription>{t("Create a populated email in your default mail app, or save an Outlook-compatible draft. Suitable recipients come from the linked Outlook event and remain visible for review here. The summary, email body, and selected attachment use the transcript language.")}</DialogDescription>
          </DialogHeader>
          {emailPreviewQuery.isLoading ? (
            <div className="grid gap-3 py-3"><div className="h-12 animate-pulse rounded-xl bg-muted" /><div className="h-40 animate-pulse rounded-xl bg-muted" /></div>
          ) : emailPreviewQuery.isError || !emailPreviewQuery.data ? (
            <p className="rounded-xl border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">{t("The email template could not be prepared.")}</p>
          ) : (
            <div className="space-y-4">
              <div className="grid gap-3 rounded-xl border border-border/70 bg-muted/25 p-3 text-sm">
                <div><p className="text-[11px] font-semibold text-muted-foreground">{t("To · from linked Outlook event")}</p><p className="mt-1 break-words">{emailPreviewQuery.data.recipients.length > 0 ? emailPreviewQuery.data.recipients.map((item) => item.name ? `${item.name} <${item.address}>` : item.address).join(", ") : t("No suitable Outlook participants were stored. Add recipients in your mail app.")}</p></div>
                <div><p className="text-[11px] font-semibold text-muted-foreground">{t("Subject")}</p><p className="mt-1 font-medium">{emailPreviewQuery.data.subject}</p></div>
              </div>
              <div>
                <p className="text-xs font-semibold">{t("Email body preview")}</p>
                <pre className="mt-2 max-h-52 overflow-y-auto whitespace-pre-wrap rounded-xl border border-border/70 bg-background p-3 font-sans text-xs leading-5 text-foreground/85">{emailPreviewQuery.data.body}</pre>
              </div>
              <fieldset>
                <legend className="text-xs font-semibold">{t("Draft attachment")}</legend>
                <div className="mt-2 grid gap-2 sm:grid-cols-4">
                  {([['', 'Body only'], ['pdf', 'PDF'], ['docx', 'Word'], ['md', 'Markdown']] as const).map(([value, label]) => (
                    <label key={value || "body"} className={`flex cursor-pointer items-center gap-2 rounded-lg border px-3 py-2 text-xs ${emailAttachment === value ? "border-primary bg-primary/5 text-foreground" : "border-border/70 text-muted-foreground hover:bg-muted/50"}`}>
                      <input type="radio" name="meeting-email-attachment" value={value} checked={emailAttachment === value} onChange={() => setEmailAttachment(value)} className="accent-primary" />
                      {value ? <Paperclip className="h-3.5 w-3.5" /> : <Mail className="h-3.5 w-3.5" />}{t(label)}
                    </label>
                  ))}
                </div>
              </fieldset>
              <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
                <Button type="button" variant="outline" onClick={() => void composeEmailBody()}>
                  <Mail className="mr-2 h-4 w-4" />{t("Open email with summary")}
                </Button>
                <Button
                  type="button"
                  disabled={exportMutation.isPending || !detail}
                  onClick={() => {
                    if (!detail) return;
                    exportMutation.mutate({
                      path: meetingEmailDraftPath(detail.id, emailAttachment),
                      fallbackName: `${detail.title || "Meeting"} - email draft.eml`,
                    });
                  }}
                >
                  {exportMutation.isPending ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Paperclip className="mr-2 h-4 w-4" />}
                  {t("Save email draft")}{emailAttachment ? ` + ${emailAttachment.toUpperCase()}` : ""}
                </Button>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>

      <Dialog open={reprocessDialogOpen} onOpenChange={(open) => {
        if (!reprocessMutation.isPending) setReprocessDialogOpen(open);
      }}>
        <DialogContent className="sm:max-w-[660px]">
          <DialogHeader>
            <DialogTitle>{t("Process this meeting again")}</DialogTitle>
            <DialogDescription>{t("Choose exactly what Scriber should rebuild. Your original recording is never changed or overwritten.")}</DialogDescription>
          </DialogHeader>
          <fieldset className="grid gap-3 py-1">
            <legend className="sr-only">{t("Choose what to process again")}</legend>
            <label className={`flex cursor-pointer items-start gap-3 rounded-xl border p-4 ${reprocessMode === "speaker_identity" ? "border-primary bg-primary/5" : "border-border/70 bg-muted/20"} ${detail?.reprocessing?.speakerIdentityAvailable === false ? "cursor-not-allowed opacity-55" : ""}`}>
              <input
                type="radio"
                name="meeting-reprocess-mode"
                value="speaker_identity"
                checked={reprocessMode === "speaker_identity"}
                disabled={!detail?.reprocessing?.speakerIdentityAvailable || reprocessMutation.isPending}
                onChange={() => setReprocessMode("speaker_identity")}
                className="mt-1 accent-primary"
              />
              <span className="min-w-0">
                <span className="flex items-center gap-2 text-sm font-semibold"><Users className="h-4 w-4 text-primary" />{t("Refresh speaker matches")}</span>
                <span className="mt-1 block text-xs leading-5 text-muted-foreground">{t("Uses the latest saved Voice Library names and participant context. Transcript wording, audio, notes, summary, and action items stay unchanged. Suggested people still require your confirmation.")}</span>
                {!detail?.reprocessing?.speakerIdentityAvailable && detail?.reprocessing?.speakerIdentityUnavailableReason && (
                  <span className="mt-2 block text-[11px] leading-4 text-amber-700 dark:text-amber-300">{t(detail.reprocessing.speakerIdentityUnavailableReason)}</span>
                )}
              </span>
            </label>
            <label className={`flex cursor-pointer items-start gap-3 rounded-xl border p-4 ${reprocessMode === "full_transcript" ? "border-primary bg-primary/5" : "border-border/70 bg-muted/20"} ${detail?.reprocessing?.fullTranscriptAvailable === false ? "cursor-not-allowed opacity-55" : ""}`}>
              <input
                type="radio"
                name="meeting-reprocess-mode"
                value="full_transcript"
                checked={reprocessMode === "full_transcript"}
                disabled={!detail?.reprocessing?.fullTranscriptAvailable || reprocessMutation.isPending}
                onChange={() => setReprocessMode("full_transcript")}
                className="mt-1 accent-primary"
              />
              <span className="min-w-0">
                <span className="flex items-center gap-2 text-sm font-semibold"><RefreshCw className="h-4 w-4 text-primary" />{t("Create a new full transcript")}</span>
                <span className="mt-1 block text-xs leading-5 text-muted-foreground">{t("Retranscribes the complete saved recording with the model currently selected in Settings, then rebuilds timestamps and speaker segments. If the result changes, manual transcript corrections and speaker confirmations are cleared so they cannot point to the wrong words. Notes and the original audio remain unchanged.")}</span>
                <span className="mt-2 block rounded-lg bg-background/70 px-2.5 py-2 font-mono text-[11px] text-foreground/80">
                  {detail?.reprocessing?.selectedFinalProvider || t("Selected provider")} · {detail?.reprocessing?.selectedFinalModel || t("Selected model")}
                </span>
                <span className="mt-1.5 block text-[11px] leading-4 text-amber-700 dark:text-amber-300">{t("This option sends the complete recording to the configured transcription provider and may incur normal provider costs.")}</span>
                {!detail?.reprocessing?.fullTranscriptAvailable && detail?.reprocessing?.fullTranscriptUnavailableReason && (
                  <span className="mt-2 block text-[11px] leading-4 text-amber-700 dark:text-amber-300">{t(detail.reprocessing.fullTranscriptUnavailableReason)}</span>
                )}
              </span>
            </label>
          </fieldset>
          {detail?.reprocessing?.unavailableReason && (
            <p className="rounded-xl border border-amber-300/60 bg-amber-500/10 px-3 py-2.5 text-xs text-amber-900 dark:text-amber-100" role="status">{t(detail.reprocessing.unavailableReason)}</p>
          )}
          {reprocessMutation.isError && <p className="rounded-xl border border-destructive/30 bg-destructive/10 px-3 py-2.5 text-xs text-destructive" role="alert">{t(reprocessMutation.error.message)}</p>}
          <div className="rounded-xl border border-border/70 bg-muted/25 px-3 py-2.5 text-xs leading-5 text-muted-foreground">
            <ShieldCheck className="mr-2 inline h-4 w-4 align-text-bottom text-primary" />
            {t("The retained source audio is read-only for this operation. You can continue using the current meeting until the new result is ready.")}
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" disabled={reprocessMutation.isPending} onClick={() => setReprocessDialogOpen(false)}>{t("Cancel")}</Button>
            <Button
              type="button"
              disabled={
                !detail
                || reprocessMutation.isPending
                || (reprocessMode === "speaker_identity" && !detail.reprocessing?.speakerIdentityAvailable)
                || (reprocessMode === "full_transcript" && !detail.reprocessing?.fullTranscriptAvailable)
              }
              onClick={() => detail && reprocessMutation.mutate({ id: detail.id, mode: reprocessMode })}
            >
              {reprocessMutation.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              {reprocessMutation.isPending ? t("Starting…") : reprocessMode === "speaker_identity" ? t("Refresh speakers") : t("Create new transcript")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AlertDialog open={Boolean(meetingPendingDelete)} onOpenChange={(open) => !open && setMeetingPendingDelete(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("Delete this meeting?")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("“{{title}}” will be removed permanently, including its transcript, generated outputs, notes, and locally retained audio. This cannot be undone.", { title: meetingPendingDelete?.title || "" })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteMeetingMutation.isPending}>{t("Cancel")}</AlertDialogCancel>
            <AlertDialogAction
              disabled={!meetingPendingDelete || deleteMeetingMutation.isPending}
              onClick={(event) => {
                event.preventDefault();
                if (meetingPendingDelete) deleteMeetingMutation.mutate(meetingPendingDelete.id);
              }}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {deleteMeetingMutation.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              {t("Delete meeting")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

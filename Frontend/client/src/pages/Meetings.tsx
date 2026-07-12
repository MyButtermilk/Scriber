import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation } from "wouter";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  AlertTriangle,
  CalendarClock,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CirclePause,
  CirclePlay,
  Download,
  FileUp,
  FileText,
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
import { apiRequest } from "@/lib/queryClient";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";
import {
  calculateMeetingElapsedMs,
  captureMeetingPlaybackRequest,
  formatMeetingOffset,
  meetingCheckpointFreshness,
  meetingTimeToAssetTimeSeconds,
  playbackSourceForSegment,
  playbackSourceForMuteState,
  type MeetingPlaybackRequest,
  type MeetingPlaybackSource,
} from "@/lib/meeting-playback";
import { useSharedWebSocket, type ScriberWebSocketMessage } from "@/contexts/WebSocketContext";
import { useToast } from "@/hooks/use-toast";
import {
  applyMeetingActionItem,
  applyMeetingCheckpointEvent,
  applyMeetingSegmentEvent,
  applyMeetingSummaryEvent,
  applyMeetingTranscriptEditedEvent,
  MEETING_HISTORY_QUERY_KEY,
} from "@/lib/meeting-cache";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
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
  MeetingProviderProfile,
  MeetingProfilesResponse,
  MeetingSegment,
  MeetingState,
  MeetingSummary,
  MeetingsResponse,
  OutlookCalendarStatus,
  SpeakerProfileSummary,
  SpeakerModelStatus,
} from "@/lib/api-types";

const OPEN_STATES = new Set<MeetingState>(["starting", "recording", "paused", "stopping", "finalizing", "analyzing"]);
const TERMINAL_MEETING_STATES = new Set<MeetingState>([
  "ready", "capture_failed", "finalization_failed", "analysis_failed", "interrupted", "discarded",
]);
type MeetingWorkspaceView = "overview" | "transcript" | "decisions" | "actions" | "questions" | "notes" | "chat";
const MEETING_WORKSPACE_VIEWS: ReadonlyArray<readonly [MeetingWorkspaceView, string]> = [
  ["overview", "Overview"], ["transcript", "Transcript"], ["decisions", "Decisions"],
  ["actions", "Action items"], ["questions", "Open questions"],
  ["notes", "Notes"], ["chat", "Ask meeting"],
];

function stateLabel(state: MeetingState): string {
  return state.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
}

function stateTone(state: MeetingState): string {
  if (state === "recording") return "border-red-300/60 bg-red-500/10 text-red-700 dark:text-red-300";
  if (["capture_failed", "finalization_failed", "analysis_failed", "interrupted"].includes(state)) {
    return "border-amber-300/60 bg-amber-500/10 text-amber-800 dark:text-amber-200";
  }
  if (state === "ready") return "border-emerald-300/60 bg-emerald-500/10 text-emerald-800 dark:text-emerald-200";
  return "border-blue-300/60 bg-blue-500/10 text-blue-800 dark:text-blue-200";
}

function formatMoment(value: string | null): string {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

const formatOffset = formatMeetingOffset;

function formatImportDuration(seconds: number | null): string {
  if (seconds == null || !Number.isFinite(seconds)) return "Duration checked during import";
  const rounded = Math.max(0, Math.round(seconds));
  const hours = Math.floor(rounded / 3600);
  const minutes = Math.floor((rounded % 3600) / 60);
  const remainder = rounded % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`
    : `${minutes}:${String(remainder).padStart(2, "0")}`;
}

function formatImportBytes(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function formatCapacity(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "Not verified";
  const hours = Math.max(0, seconds) / 3_600;
  if (hours >= 10) return `${Math.floor(hours)} h`;
  if (hours >= 1) return `${hours.toFixed(1)} h`;
  return `${Math.max(1, Math.floor(seconds / 60))} min`;
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
  return (
    <section className="my-1 border-y border-border/55 py-2" aria-labelledby="meeting-import-inbox-title">
      <div className="flex items-center justify-between gap-2 px-2 py-1">
        <div className="min-w-0">
          <p id="meeting-import-inbox-title" className="text-xs font-semibold">Imports</p>
          <p className="text-[11px] text-muted-foreground">Durable work across restarts</p>
        </div>
        {!loading && !error && items.length > 0 && (
          <span className="rounded-full bg-muted px-2 py-0.5 font-mono text-[10px] tabular-nums text-muted-foreground">
            {items.length}
          </span>
        )}
      </div>
      {loading ? (
        <div className="mt-1 grid gap-1 px-1 sm:grid-cols-2 lg:grid-cols-3 min-[1100px]:grid-cols-1" aria-label="Loading meeting imports">
          {[0, 1].map((item) => <div key={item} className="h-[76px] animate-pulse rounded-xl bg-muted/60" />)}
        </div>
      ) : error ? (
        <div className="mt-1 flex items-center justify-between gap-2 px-2 py-2 text-xs text-destructive" role="alert">
          <span>Imports could not be loaded.</span>
          <Button type="button" size="sm" variant="ghost" className="h-7 px-2 text-xs" onClick={onRefresh}>
            <RefreshCw className="mr-1.5 h-3 w-3" />Retry
          </Button>
        </div>
      ) : items.length === 0 ? (
        <p className="px-2 py-2 text-xs text-muted-foreground">No pending or recently interrupted imports.</p>
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
                      <span className={`truncate font-medium ${importStateTone(job.state)}`}>{job.status}</span>
                      <span className="shrink-0 text-muted-foreground">{formatMoment(job.updatedAt)}</span>
                    </div>
                  </div>
                </div>
                {active && (
                  <div className="mt-2 h-1 overflow-hidden rounded-full bg-muted" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(job.progress * 100)} aria-label={`${job.title} import progress`}>
                    <div className="h-full origin-left rounded-full bg-primary transition-transform duration-200 motion-reduce:transition-none" style={{ transform: `scaleX(${Math.max(0.02, Math.min(1, job.progress))})` }} />
                  </div>
                )}
                {job.errorMessage && <p className="mt-1.5 line-clamp-2 text-[10px] leading-4 text-muted-foreground">{job.errorMessage}</p>}
                {(job.meetingId || job.canCancel) && (
                  <div className="mt-2 flex flex-wrap items-center gap-1">
                    {job.meetingId && (
                      <Button type="button" size="sm" variant="ghost" className="h-7 px-2 text-[11px] active:scale-[0.97]" onClick={() => onOpen(job.meetingId!)}>
                        Open meeting
                      </Button>
                    )}
                    {job.canRetry && job.meetingId && (
                      <Button type="button" size="sm" variant="outline" className="h-7 px-2 text-[11px] active:scale-[0.97]" disabled={retrying} onClick={() => onRetry(job.meetingId!)}>
                        {retrying && <Loader2 className="mr-1.5 h-3 w-3 animate-spin" />}Retry
                      </Button>
                    )}
                    {job.canCancel && (
                      <Button type="button" size="sm" variant="ghost" className="h-7 px-2 text-[11px] text-muted-foreground hover:text-destructive active:scale-[0.97]" disabled={canceling} onClick={() => onCancel(job.id)}>
                        {canceling && <Loader2 className="mr-1.5 h-3 w-3 animate-spin" />}Cancel
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
  onPlay,
  canEdit,
  savingSegmentId,
  onSave,
  onUndo,
}: {
  segments: DisplayMeetingSegment[];
  search: string;
  hasPlayableAudio: boolean;
  onPlay: (source: "microphone" | "system" | "mixed", startMs: number) => void;
  canEdit: boolean;
  savingSegmentId: string;
  onSave: (segment: DisplayMeetingSegment, text: string) => void;
  onUndo: (segment: DisplayMeetingSegment) => void;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
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
  return (
    <div ref={scrollRef} className="max-h-[520px] overflow-y-auto pr-2" aria-label="Meeting transcript segments">
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
                  onClick={() => onPlay(segment.source, segment.startMs)}
                  disabled={!hasPlayableAudio}
                  className="self-start rounded-lg text-left text-[10px] tabular-nums outline-none enabled:active:scale-[0.98] focus-visible:ring-2 focus-visible:ring-primary disabled:cursor-default"
                  title={hasPlayableAudio ? `Play ${formatOffset(segment.startMs)} to ${formatOffset(segment.endMs)}` : "Saved audio is unavailable"}
                  aria-label={`Play transcript segment from ${formatOffset(segment.startMs)} to ${formatOffset(segment.endMs)}`}
                >
                  <span className="grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5 font-mono">
                    <span className="font-sans text-muted-foreground">Start</span>
                    <span className="text-right font-medium text-primary">{formatOffset(segment.startMs)}</span>
                    <span className="font-sans text-muted-foreground">End</span>
                    <span className="text-right font-medium text-primary">{formatOffset(segment.endMs)}</span>
                    <span className="font-sans text-muted-foreground">Duration</span>
                    <span className="text-right text-muted-foreground">{(segment.durationMs / 1000).toFixed(1)} s</span>
                  </span>
                  {segment.alignmentQuality === "estimated" && (
                    <span className="mt-1 block font-sans text-[9px] font-medium uppercase tracking-wide text-amber-700 dark:text-amber-300" title="This interval was estimated because the transcription provider returned no exact word timing.">
                      Estimated timing
                    </span>
                  )}
                </button>
                <div className="min-w-0">
                  <div className="flex min-w-0 items-center gap-2">
                    <span className="truncate text-xs font-semibold text-muted-foreground">{highlightTranscriptMatch(segment.label, search)}</span>
                    {segment.editVersion > 0 && <Badge variant="outline" className="h-5 shrink-0 px-1.5 text-[9px]">Edited</Badge>}
                  </div>
                  {editingId === segment.id ? (
                    <div className="mt-2 space-y-2">
                      <Textarea
                        value={draft}
                        onChange={(event) => setDraft(event.target.value)}
                        rows={3}
                        autoFocus
                        aria-label={`Edit transcript for ${segment.label} at ${formatOffset(segment.startMs)}`}
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
                          {savingSegmentId === segment.id && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />}Save correction
                        </Button>
                        <Button type="button" size="sm" variant="ghost" className="h-8 active:scale-[0.97]" onClick={cancelEdit}>
                          <X className="mr-1.5 h-3.5 w-3.5" />Cancel
                        </Button>
                        <span className="text-[10px] text-muted-foreground">Ctrl+Enter saves · Esc cancels</span>
                      </div>
                    </div>
                  ) : (
                    <>
                      <p className="mt-1 text-sm leading-6">{highlightTranscriptMatch(segment.text, search)}</p>
                      {canEdit && (
                        <div className="mt-2 flex items-center gap-1 opacity-100 sm:opacity-0 sm:group-hover:opacity-100 sm:group-focus-within:opacity-100">
                          <Button type="button" size="sm" variant="ghost" className="h-7 px-2 text-[11px] active:scale-[0.97]" onClick={() => beginEdit(segment)}>
                            <Pencil className="mr-1.5 h-3 w-3" />Edit
                          </Button>
                          {segment.editVersion > 0 && (
                            <Button type="button" size="sm" variant="ghost" className="h-7 px-2 text-[11px] active:scale-[0.97]" disabled={savingSegmentId === segment.id} onClick={() => onUndo(segment)}>
                              <Undo2 className="mr-1.5 h-3 w-3" />Undo latest
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
  );
});

function EvidenceList({ items, onCitation }: { items: unknown; onCitation?: (id: string) => void }) {
  if (!Array.isArray(items) || items.length === 0) {
    return <p className="py-12 text-center text-sm text-muted-foreground">Nothing was identified with sufficient transcript evidence.</p>;
  }
  return <div className="divide-y divide-border/60">{items.map((raw, index) => {
    const item = raw && typeof raw === "object" ? raw as Record<string, unknown> : { text: String(raw) };
    const citations = Array.isArray(item.segmentIds) ? item.segmentIds.map(String) : [];
    return <div key={`${String(item.text)}-${index}`} className="py-4">
      <p className="text-sm leading-6">{String(item.text || item.summary || item.title || "")}</p>
      {Boolean(item.owner || item.dueDate) && <p className="mt-1 text-xs text-muted-foreground">{item.owner ? `Owner: ${String(item.owner)}` : "Unassigned"}{item.dueDate ? ` · Due ${String(item.dueDate)}` : ""}</p>}
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
  if (items.length === 0) {
    return <p className="py-12 text-center text-sm text-muted-foreground">No evidence-backed action items were identified.</p>;
  }
  return <div className="divide-y divide-border/60" aria-busy={saving}>{items.map((item) => (
    <div key={`${item.id}:${item.updatedAt}`} className="grid gap-3 py-4 sm:grid-cols-[32px_minmax(0,1fr)]">
      <button
        type="button"
        disabled={saving}
        onClick={() => onChange(item, { status: item.status === "done" ? "open" : "done" })}
        className={`mt-1 flex h-6 w-6 items-center justify-center rounded-full border active:scale-[0.97] ${item.status === "done" ? "border-emerald-500 bg-emerald-500 text-white" : "border-border hover:border-primary"}`}
        aria-label={item.status === "done" ? "Reopen action item" : "Complete action item"}
      >{item.status === "done" && <Check className="h-3.5 w-3.5" />}</button>
      <div className="min-w-0 space-y-2">
        <Input
          disabled={saving}
          defaultValue={item.text}
          className={`h-auto border-0 bg-transparent px-0 py-0 text-sm shadow-none focus-visible:ring-0 ${item.status === "done" ? "text-muted-foreground line-through" : ""}`}
          onBlur={(event) => event.target.value.trim() !== item.text && onChange(item, { text: event.target.value })}
        />
        <div className="grid gap-2 sm:grid-cols-2">
          <Input disabled={saving} defaultValue={item.owner ?? ""} placeholder="Owner" className="h-8 text-xs" onBlur={(event) => event.target.value !== (item.owner ?? "") && onChange(item, { owner: event.target.value || null })} />
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

async function downloadApiFile(path: string, fallbackName: string): Promise<string> {
  const response = await fetchWithTimeout(apiUrl(path), { credentials: "include" }, 60_000);
  if (!response.ok) throw new Error(`Download failed (${response.status})`);
  const disposition = response.headers.get("Content-Disposition") || "";
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  const plain = disposition.match(/filename="([^"]+)"/i)?.[1];
  const filename = encoded ? decodeURIComponent(encoded) : plain || fallbackName;
  const objectUrl = URL.createObjectURL(await response.blob());
  try {
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = filename;
    anchor.style.display = "none";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1_000);
  }
  return filename;
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
    <div className="order-first text-left sm:order-none sm:text-center" aria-label={`Meeting elapsed time ${formatOffset(elapsedMs)}`}>
      <p className="font-mono text-2xl font-semibold tabular-nums tracking-tight">{formatOffset(elapsedMs)}</p>
      <p className="mt-0.5 text-[10px] font-semibold uppercase tracking-[0.14em] text-primary">
        {paused ? "Capture paused" : "Live capture"}
      </p>
      {showProviderLimit && <p className="mt-1 text-[10px] font-semibold text-amber-700 dark:text-amber-300" role="status">
        {providerRemainingMs > 0
          ? `Final STT limit in ${formatOffset(providerRemainingMs)}`
          : "Final STT duration limit reached"}
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
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!checkpoint || paused) return;
    setNow(Date.now());
    const handle = window.setInterval(() => setNow(Date.now()), 5_000);
    return () => window.clearInterval(handle);
  }, [checkpoint, paused]);
  if (!checkpoint) {
    return <span>First protected checkpoint at 0:30</span>;
  }
  const freshness = meetingCheckpointFreshness(checkpoint.updatedAt, now, paused);
  return (
    <span className={freshness.stale ? "text-amber-700 dark:text-amber-300" : undefined}>
      Saved through {formatOffset(checkpoint.cutoffMs)} · {freshness.ageLabel} · {checkpoint.sources.length}/{expectedTrackCount} tracks
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
          <span className="font-medium">{source === "microphone" ? "Microphone" : "System audio"}</span>
          <span className="text-emerald-600 dark:text-emerald-300">{paused ? "Paused" : "Healthy"}</span>
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
        aria-label="Meeting workspace views"
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
            {label}
          </button>
        ))}
      </nav>
      <button
        type="button"
        onClick={() => scroll(-1)}
        disabled={!overflow.left}
        className="absolute inset-y-0 left-0 grid w-10 place-items-center border-r border-border/50 bg-card/95 text-muted-foreground disabled:opacity-30"
        aria-label="Previous meeting views"
      >
        <ChevronLeft className="h-4 w-4" />
      </button>
      <button
        type="button"
        onClick={() => scroll(1)}
        disabled={!overflow.right}
        className="absolute inset-y-0 right-0 grid w-10 place-items-center border-l border-border/50 bg-card/95 text-muted-foreground disabled:opacity-30"
        aria-label="More meeting views"
      >
        <ChevronRight className="h-4 w-4" />
      </button>
    </div>
  );
});

export default function Meetings({ params }: { params?: { id?: string } }) {
  const selectedId = params?.id || "";
  const [, setLocation] = useLocation();
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [title, setTitle] = useState("");
  const [voiceLibraryEnabled, setVoiceLibraryEnabled] = useState(false);
  const [audioRetentionDays, setAudioRetentionDays] = useState(0);
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
  const silencedPlaybackRef = useRef<MeetingPlaybackRequest | null>(null);
  const [audioSource, setAudioSource] = useState<MeetingPlaybackSource>("mix");
  const [playbackError, setPlaybackError] = useState("");
  const [mutedSources, setMutedSources] = useState({ microphone: false, system: false });
  const [meetingPendingDelete, setMeetingPendingDelete] = useState<MeetingSummary | null>(null);
  const [transcriptSearch, setTranscriptSearch] = useState("");
  const [emailDialogOpen, setEmailDialogOpen] = useState(false);
  const [meetingImportCandidate, setMeetingImportCandidate] = useState<{
    file: File;
    title: string;
    durationSeconds: number | null;
  } | null>(null);
  const [meetingImportProfileId, setMeetingImportProfileId] = useState("");
  const [meetingImportId, setMeetingImportId] = useState("");
  const [meetingImportProgress, setMeetingImportProgress] = useState({ stage: "Ready", percentage: 0 });
  const [emailAttachment, setEmailAttachment] = useState<"" | "md" | "pdf" | "docx">("pdf");
  const [mergeTargetProfileId, setMergeTargetProfileId] = useState("");
  const [mergeSourceProfileId, setMergeSourceProfileId] = useState("");
  const [retryFinalProvider, setRetryFinalProvider] = useState("");
  const [profileId, setProfileId] = useState("");
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
  const [meetingProgress, setMeetingProgress] = useState<{ phase: "finalize" | "analysis"; progress: number; status: string } | null>(null);
  const [liveStatuses, setLiveStatuses] = useState<Record<"microphone" | "system", { status: "reconnecting" | "recovered" | "degraded"; reconnectCount: number } | null>>({ microphone: null, system: null });
  const meetingWsHasConnectedRef = useRef(false);
  const meetingWsWasConnectedRef = useRef(false);

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
    queryKey: ["/api/meeting-imports"],
    queryFn: ({ signal }) => fetchJson("/api/meeting-imports?limit=24", signal),
    staleTime: 5_000,
    refetchInterval: meetingImportId ? 2_000 : false,
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
    refetchInterval: selectedId ? false : 15_000,
  });
  const detectionQuery = useQuery<MeetingDetectionResponse>({
    queryKey: ["/api/meetings/detection"],
    queryFn: ({ signal }) => fetchJson("/api/meetings/detection", signal),
    staleTime: 2_000,
    refetchInterval: selectedId ? false : 5_000,
  });
  const outlookQuery = useQuery<OutlookCalendarStatus>({
    queryKey: ["/api/calendar/outlook/status"],
    queryFn: ({ signal }) => fetchJson("/api/calendar/outlook/status", signal),
    staleTime: 15_000,
    refetchInterval: (query) => query.state.data?.authorizationPending ? 2_000 : 30_000,
  });
  const speakerModelQuery = useQuery<SpeakerModelStatus>({
    queryKey: ["/api/meetings/speaker-model"],
    queryFn: ({ signal }) => fetchJson("/api/meetings/speaker-model", signal),
    staleTime: 30_000,
  });
  const speakerProfilesQuery = useQuery<{ items: SpeakerProfileSummary[] }>({
    queryKey: ["/api/meetings/speaker-profiles"],
    queryFn: ({ signal }) => fetchJson("/api/meetings/speaker-profiles", signal),
    staleTime: 15_000,
  });
  const detailQuery = useQuery<MeetingDetail>({
    queryKey: ["/api/meetings", selectedId],
    queryFn: ({ signal }) => fetchJson(`/api/meetings/${selectedId}`, signal),
    enabled: Boolean(selectedId),
    staleTime: 30_000,
  });
  const deliveriesQuery = useQuery<{ items: Array<{ id: string; target: string; status: string; attemptCount: number }> }>({
    queryKey: ["/api/meetings", selectedId, "deliveries"],
    queryFn: ({ signal }) => fetchJson(`/api/meetings/${selectedId}/deliveries`, signal),
    enabled: Boolean(selectedId),
    staleTime: 5_000,
  });
  const detail = detailQuery.data;
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
    if (!profileId && profilesQuery.data?.defaultProfileId) {
      setProfileId(profilesQuery.data.defaultProfileId);
    }
  }, [profileId, profilesQuery.data?.defaultProfileId]);

  useEffect(() => {
    const profile = profilesQuery.data?.profiles.find((item) => item.id === profileId);
    if (profile) setAudioRetentionDays(profile.audioRetentionDays);
  }, [profileId, profilesQuery.data?.profiles]);

  useEffect(() => {
    setEmailDialogOpen(false);
    setEmailAttachment("pdf");
    setWebhookUrl("");
    setWebhookPreview(null);
    setWebhookConfirmed(false);
    setWebhookSecret("");
    audioLevelsRef.current = { microphone: 0, system: 0 };
    setMeetingProgress(null);
    setLiveStatuses({ microphone: null, system: null });
    pendingPlaybackRef.current = null;
    silencedPlaybackRef.current = null;
    audioRef.current?.pause();
    setAudioSource("mix");
    setMutedSources({ microphone: false, system: false });
    setChatQuestion("");
    setChatAnswer(null);
    setTranscriptSearch("");
    setRetryFinalProvider("");
    setNote("");
    setNoteHydratedFor("");
  }, [selectedId]);

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

  const invalidateMeetings = useCallback((meetingId?: string) => {
    void queryClient.invalidateQueries({ queryKey: ["/api/meetings"] });
    void queryClient.invalidateQueries({ queryKey: ["/api/meetings/capabilities"] });
    if (meetingId) void queryClient.invalidateQueries({ queryKey: ["/api/meetings", meetingId] });
  }, [queryClient]);

  const invalidateMeetingImports = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ["/api/meeting-imports"] });
  }, [queryClient]);

  const handleWsMessage = useCallback((message: ScriberWebSocketMessage) => {
    if (message.type === "meeting_state") {
      applyMeetingSummaryEvent(queryClient, message.meeting);
      if (TERMINAL_MEETING_STATES.has(message.meeting.state)) {
        void queryClient.invalidateQueries({ queryKey: ["/api/meetings", message.meeting.id], exact: true });
        void queryClient.invalidateQueries({ queryKey: ["/api/meetings/speaker-profiles"] });
      }
      if (message.meeting.id === selectedId && !["stopping", "finalizing", "analyzing"].includes(message.meeting.state)) {
        setMeetingProgress(null);
      }
      if (message.meeting.id === selectedId && TERMINAL_MEETING_STATES.has(message.meeting.state)) {
        audioLevelsRef.current = { microphone: 0, system: 0 };
      }
    }
    if ((message.type === "meeting_finalize_progress" || message.type === "meeting_analysis_progress") && message.meetingId === selectedId) {
      setMeetingProgress({
        phase: message.type === "meeting_analysis_progress" ? "analysis" : "finalize",
        progress: Math.max(0, Math.min(1, message.progress)),
        status: message.status,
      });
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
    if (message.type === "meeting_note") invalidateMeetings(message.meetingId);
    if (message.type === "meeting_import_progress") {
      invalidateMeetingImports();
      if (message.importId === meetingImportId) {
        setMeetingImportProgress({
          stage: message.status,
          percentage: Math.round(Math.max(0, Math.min(1, message.progress)) * 100),
        });
        if (message.meetingId) {
          meetingImportIdRef.current = "";
          setMeetingImportId("");
          setMeetingImportCandidate(null);
          invalidateMeetings(message.meetingId);
          setLocation(`/meetings/${message.meetingId}`);
          toast({ title: "Meeting workspace created", description: "Final transcription and speaker processing are running." });
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
  }, [invalidateMeetingImports, invalidateMeetings, meetingImportId, queryClient, selectedId, setLocation, toast]);
  const { isConnected } = useSharedWebSocket(handleWsMessage);

  useEffect(() => {
    if (isConnected && meetingWsHasConnectedRef.current && !meetingWsWasConnectedRef.current) {
      invalidateMeetings(selectedId || undefined);
      invalidateMeetingImports();
    }
    if (isConnected) meetingWsHasConnectedRef.current = true;
    meetingWsWasConnectedRef.current = isConnected;
  }, [invalidateMeetingImports, invalidateMeetings, isConnected, selectedId]);

  useEffect(() => {
    const job = meetingImportsQuery.data?.items.find((item) => item.id === meetingImportId);
    if (!job) return;
    setMeetingImportProgress({
      stage: job.status || job.state,
      percentage: Math.round(Math.max(0, Math.min(1, job.progress)) * 100),
    });
    if (job.meetingId) {
      meetingImportIdRef.current = "";
      setMeetingImportId("");
      setMeetingImportCandidate(null);
      invalidateMeetings(job.meetingId);
      setLocation(`/meetings/${job.meetingId}`);
      toast({ title: "Meeting workspace created", description: "The durable import was reconciled from server state." });
    } else if (["failed", "canceled"].includes(job.state)) {
      meetingImportIdRef.current = "";
      meetingImportExplicitCancelRef.current = false;
      setMeetingImportId("");
      if (job.state === "canceled") setMeetingImportCandidate(null);
    }
  }, [invalidateMeetings, meetingImportId, meetingImportsQuery.data?.items, setLocation, toast]);

  const startMutation = useMutation({
    mutationFn: async () => {
      const profile = profilesQuery.data?.profiles.find((item) => item.id === profileId);
      const response = await apiRequest("POST", "/api/meetings", {
        title,
        language: profile?.language ?? "auto",
        liveProvider: profile?.liveProvider ?? "soniox",
        finalProvider: profile?.finalProvider ?? "soniox_async",
        analysisModel: profile?.analysisModel ?? "",
        aecEnabled: profile?.aecEnabled ?? true,
        voiceLibraryEnabled,
        audioRetentionDays,
        smartTurnEnabled: profile?.smartTurnEnabled ?? true,
        autoAnalyze: profile?.autoAnalyze ?? true,
        microphoneNativeEndpointIdHash: microphoneEndpointHash,
        renderNativeEndpointIdHash: renderEndpointHash,
      });
      return response.json() as Promise<MeetingSummary>;
    },
    onSuccess: (meeting) => {
      setTitle("");
      setVoiceLibraryEnabled(false);
      invalidateMeetings(meeting.id);
      setLocation(`/meetings/${meeting.id}`);
    },
    onError: (error) => toast({ variant: "destructive", title: "Meeting could not start", description: error.message }),
  });
  const deviceTestMutation = useMutation({
    mutationFn: async () => {
      const profile = profilesQuery.data?.profiles.find((item) => item.id === profileId);
      const response = await apiRequest("POST", "/api/meetings/device-test", {
        microphoneNativeEndpointIdHash: microphoneEndpointHash,
        renderNativeEndpointIdHash: renderEndpointHash,
        aecEnabled: profile?.aecEnabled ?? true,
        durationMs: 3_000,
        playTestTone: true,
      });
      return response.json() as Promise<MeetingDeviceTestResponse>;
    },
    onError: (error) => toast({ variant: "destructive", title: "Audio routes could not be tested", description: error.message }),
  });
  const detectionDismissMutation = useMutation({
    mutationFn: async (detectionId: string) => {
      const response = await apiRequest("POST", "/api/meetings/detection/dismiss", { detectionId });
      return response.json();
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["/api/meetings/detection"] }),
    onError: (error) => toast({ variant: "destructive", title: "Suggestion could not be dismissed", description: error.message }),
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
        toast({ variant: "destructive", title: "Webhook preview failed", description: error.message });
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
      toast({ title: "Webhook delivered" });
    },
    onError: (error, variables) => {
      if (variables.id === selectedId) {
        toast({ variant: "destructive", title: "Webhook delivery failed", description: error.message });
      }
    },
  });
  const outlookMutation = useMutation({
    mutationFn: async (action: "connect" | "sync" | "disconnect") => {
      const response = action === "disconnect"
        ? await apiRequest("DELETE", "/api/calendar/outlook")
        : await apiRequest("POST", `/api/calendar/outlook/${action}`, action === "connect" ? { openBrowser: true } : undefined);
      return response.json();
    },
    onSuccess: (_payload, action) => {
      void queryClient.invalidateQueries({ queryKey: ["/api/calendar/outlook/status"] });
      toast({ title: action === "connect" ? "Continue in your browser" : action === "sync" ? "Calendar synchronized" : "Outlook disconnected" });
    },
    onError: (error) => toast({ variant: "destructive", title: "Outlook action failed", description: error.message }),
  });
  const speakerModelMutation = useMutation({
    mutationFn: async () => {
      const response = await apiRequest("POST", "/api/meetings/speaker-model");
      return response.json() as Promise<SpeakerModelStatus>;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["/api/meetings/speaker-model"] });
      toast({ title: "Speaker model installed", description: "The checksum-verified model is ready for local Voice Library processing." });
    },
    onError: (error) => toast({ variant: "destructive", title: "Model installation failed", description: error.message }),
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
      invalidateMeetingImports();
      setMeetingImportProgress({ stage: "Uploading recording", percentage: 2 });
      return new Promise<MeetingImportJob>((resolve, reject) => {
        const request = new XMLHttpRequest();
        request.open("PUT", apiUrl(created.uploadUrl || `/api/meeting-imports/${created.id}/content`));
        request.withCredentials = true;
        request.timeout = 10 * 60 * 1000;
        request.setRequestHeader("Content-Type", file.type || "application/octet-stream");
        request.upload.onprogress = (event) => {
          if (!event.lengthComputable) return;
          const uploadProgress = Math.max(2, Math.min(85, Math.round((event.loaded / event.total) * 85)));
          setMeetingImportProgress({ stage: "Uploading recording", percentage: uploadProgress });
        };
        request.upload.onload = () => setMeetingImportProgress({ stage: "Safely committing upload", percentage: 85 });
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
          setMeetingImportProgress({ stage: "Upload safely stored", percentage: 86 });
          resolve(payload);
        };
        request.send(file);
      });
    },
    onSuccess: () => {
      invalidateMeetingImports();
      toast({
        title: "Recording safely uploaded",
        description: "Scriber is preparing the durable Meeting workspace.",
      });
    },
    onError: (error) => {
      const importId = meetingImportIdRef.current;
      if (
        meetingImportExplicitCancelRef.current
        || (error instanceof DOMException && error.name === "AbortError")
      ) {
        invalidateMeetingImports();
        setMeetingImportProgress({ stage: "Cancel requested", percentage: 0 });
        return;
      }
      meetingImportIdRef.current = "";
      setMeetingImportId("");
      setMeetingImportCandidate(null);
      invalidateMeetingImports();
      toast({
        variant: "destructive",
        title: "Meeting import needs attention",
        description: importId
          ? "The upload response was interrupted. Its durable server state remains in Imports and will be reconciled there."
          : error.message,
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
      if (
        meetingImportIdRef.current === job.id
        || (job.state === "canceled" && !meetingImportIdRef.current)
      ) {
        meetingImportIdRef.current = "";
        meetingImportExplicitCancelRef.current = false;
        setMeetingImportId("");
        setMeetingImportCandidate(null);
      }
      invalidateMeetingImports();
      toast({ title: job.state === "canceled" ? "Meeting import canceled" : "Cancellation requested" });
    },
    onError: (error) => {
      meetingImportExplicitCancelRef.current = false;
      invalidateMeetingImports();
      toast({ variant: "destructive", title: "Meeting import could not be canceled", description: error.message });
    },
  });

  const controlMutation = useMutation({
    mutationFn: async ({ id, action }: { id: string; action: "pause" | "resume" | "stop" }) => {
      const response = await apiRequest("POST", `/api/meetings/${id}/${action}`);
      return response.json() as Promise<MeetingSummary>;
    },
    onSuccess: (meeting) => invalidateMeetings(meeting.id),
    onError: (error) => toast({ variant: "destructive", title: "Meeting control failed", description: error.message }),
  });

  const deleteMeetingMutation = useMutation({
    mutationFn: async (id: string) => {
      await apiRequest("DELETE", `/api/meetings/${id}`);
      return id;
    },
    onSuccess: (id) => {
      setMeetingPendingDelete(null);
      queryClient.removeQueries({ queryKey: ["/api/meetings", id] });
      invalidateMeetings();
      if (selectedId === id) setLocation("/meetings");
      toast({ title: "Meeting deleted", description: "Transcript, generated outputs, and locally retained audio were removed." });
    },
    onError: (error) => toast({ variant: "destructive", title: "Meeting could not be deleted", description: error.message }),
  });

  const exportMutation = useMutation({
    mutationFn: async ({ path, fallbackName }: { path: string; fallbackName: string }) => (
      downloadApiFile(path, fallbackName)
    ),
    onSuccess: (filename) => toast({ title: "Export ready", description: filename }),
    onError: (error) => toast({ variant: "destructive", title: "Export failed", description: error.message }),
  });

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
      return response.json();
    },
    onSuccess: (_payload, variables) => {
      if (noteDraftRef.current.meetingId === variables.id) {
        lastSavedNote.current = variables.body;
        noteDraftRef.current.savedBody = variables.body;
      }
      invalidateMeetings(variables.id);
    },
    onError: (error) => toast({ variant: "destructive", title: "Note was not saved", description: error.message }),
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
        toast({ variant: "destructive", title: "Note was not saved", description: error.message });
      });
    }
  }, [selectedId, toast]);
  const actionItemMutation = useMutation({
    scope: { id: "meeting-action-item-updates" },
    mutationFn: async ({ id, itemId, changes }: { id: string; itemId: string; changes: Record<string, unknown> }) => {
      const response = await apiRequest("PATCH", `/api/meetings/${id}/action-items/${itemId}`, changes);
      return response.json() as Promise<MeetingActionItem>;
    },
    onSuccess: (item, variables) => {
      applyMeetingActionItem(queryClient, variables.id, item);
      invalidateMeetings(variables.id);
    },
    onError: (error) => toast({ variant: "destructive", title: "Action item was not saved", description: error.message }),
  });
  const analysisMutation = useMutation({
    mutationFn: async (id: string) => {
      const response = await apiRequest("POST", `/api/meetings/${id}/analyze`);
      return response.json() as Promise<MeetingSummary>;
    },
    onSuccess: (meeting) => invalidateMeetings(meeting.id),
    onError: (error) => toast({ variant: "destructive", title: "Analysis could not start", description: error.message }),
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
        title: "Transcript corrected",
        description: result.outputsStale
          ? "The existing meeting brief is marked as based on an older transcript."
          : "Search, playback links, and new exports now use the correction.",
      });
    },
    onError: (error) => toast({
      variant: "destructive",
      title: "Transcript correction was not saved",
      description: error.message,
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
    onSuccess: (_payload, variables) => {
      invalidateMeetings(variables.id);
      invalidateMeetingImports();
      if (variables.action === "discard") setLocation("/meetings");
    },
    onError: (error) => toast({ variant: "destructive", title: "Recovery action failed", description: error.message }),
  });
  const speakerMutation = useMutation({
    mutationFn: async ({ id, speakerId, displayName }: { id: string; speakerId: string; displayName: string }) => {
      const response = await apiRequest("PATCH", `/api/meetings/${id}/speakers/${speakerId}`, { displayName });
      return response.json();
    },
    onSuccess: (_payload, variables) => invalidateMeetings(variables.id),
    onError: (error) => toast({ variant: "destructive", title: "Speaker name was not saved", description: error.message }),
  });
  const splitSpeakerMutation = useMutation({
    mutationFn: async ({ id, speakerId }: { id: string; speakerId: string }) => {
      const response = await apiRequest("POST", `/api/meetings/${id}/speakers/${speakerId}/split-profile`);
      return response.json();
    },
    onSuccess: (_payload, variables) => {
      invalidateMeetings(variables.id);
      void queryClient.invalidateQueries({ queryKey: ["/api/meetings/speaker-profiles"] });
      toast({ title: "Speaker profile separated" });
    },
    onError: (error) => toast({ variant: "destructive", title: "Profile could not be separated", description: error.message }),
  });
  const mergeProfilesMutation = useMutation({
    mutationFn: async () => {
      const response = await apiRequest("POST", "/api/meetings/speaker-profiles/merge", {
        targetProfileId: mergeTargetProfileId,
        sourceProfileId: mergeSourceProfileId,
      });
      return response.json();
    },
    onSuccess: () => {
      setMergeSourceProfileId("");
      void queryClient.invalidateQueries({ queryKey: ["/api/meetings/speaker-profiles"] });
      invalidateMeetings(selectedId);
      toast({ title: "Speaker profiles merged" });
    },
    onError: (error) => toast({ variant: "destructive", title: "Profiles could not be merged", description: error.message }),
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
        toast({ variant: "destructive", title: "Meeting chat failed", description: error.message });
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
  const selectedProfile = profilesQuery.data?.profiles.find((item) => item.id === profileId);
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
    ? `Max ${formatImportDuration(finalProviderCapability.maxDurationSeconds)}`
    : finalProviderCapability?.fiveHourSupported
      ? "5 h compatible"
      : "Not 5 h compatible";
  const meetingImportProfile = profilesQuery.data?.profiles.find((item) => item.id === meetingImportProfileId)
    ?? profilesQuery.data?.profiles.find((item) => item.id === profilesQuery.data?.defaultProfileId);
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
      || !selectedProfile?.available,
  );
  const meetingImportBusy = meetingImportMutation.isPending || Boolean(meetingImportId);
  const liveSegments = detail?.segments ?? [];
  const groupedSegments = useMemo(() => liveSegments.map((segment) => ({
    ...segment,
    label: segment.speakerLabel || (segment.source === "microphone" ? "You" : "Meeting audio"),
  })), [liveSegments]);
  const visibleTranscriptSegments = useMemo(() => {
    const query = transcriptSearch.trim().toLocaleLowerCase();
    if (!query) return groupedSegments;
    return groupedSegments.filter((segment) => (
      segment.text.toLocaleLowerCase().includes(query)
      || segment.label.toLocaleLowerCase().includes(query)
    ));
  }, [groupedSegments, transcriptSearch]);
  const analysisOutput = detail?.outputs.find((output) => output.kind === "analysis" && output.status === "completed");
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
        title: "Transcript is not ready",
        description: "Wait for the final transcript before generating the meeting brief.",
      });
      return;
    }
    analysisMutation.mutate(detail.id);
  };
  const availablePlaybackSources = useMemo(() => new Set<MeetingPlaybackSource>(
    (detail?.audioAssets ?? []).flatMap((asset) => (
      asset.kind === "playback_mix" ? ["mix" as const]
      : asset.kind === "playback_microphone" ? ["microphone" as const]
      : asset.kind === "playback_system" ? ["system" as const]
      : []
    )),
  ), [detail?.audioAssets]);
  const hasPlayableAudio = availablePlaybackSources.size > 0;
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
        audioSource,
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
  }, [audioSource, detail?.audioAssets]);
  const captureCurrentPlayback = useCallback((): MeetingPlaybackRequest => {
    const audio = audioRef.current;
    if (!audio) return { meetingTimeMs: 0, shouldPlay: false };
    return captureMeetingPlaybackRequest(
      audio.currentTime,
      audio.paused,
      audio.ended,
      detail?.audioAssets,
      audioSource,
    );
  }, [audioSource, detail?.audioAssets]);
  const playSegment = useCallback((source: "microphone" | "system" | "mixed", startMs: number) => {
    if (!detail) return;
    const requestedSource = playbackSourceForSegment(source);
    const nextSource = availablePlaybackSources.has(requestedSource)
      ? requestedSource
      : availablePlaybackSources.has("mix") ? "mix" : null;
    if (!nextSource) {
      setPlaybackError("Saved audio is not available for this meeting.");
      return;
    }
    const request: MeetingPlaybackRequest = {
      meetingTimeMs: Math.max(0, startMs),
      shouldPlay: true,
    };
    silencedPlaybackRef.current = null;
    setPlaybackError("");
    setMutedSources(nextSource === "mix" || source === "mixed"
      ? { microphone: false, system: false }
      : source === "microphone"
      ? { microphone: false, system: true }
      : { microphone: true, system: false });
    if (audioSource === nextSource) {
      if ((audioRef.current?.readyState ?? 0) >= 1) {
        pendingPlaybackRef.current = null;
        void playLoadedAudio(request);
      } else {
        pendingPlaybackRef.current = request;
        audioRef.current?.load();
      }
    } else {
      pendingPlaybackRef.current = request;
      setAudioSource(nextSource);
    }
  }, [audioSource, availablePlaybackSources, detail, playLoadedAudio]);
  const togglePlaybackSource = useCallback((source: "microphone" | "system") => {
    setMutedSources((current) => ({ ...current, [source]: !current[source] }));
  }, []);
  useEffect(() => {
    const audio = audioRef.current;
    if (mutedSources.microphone && mutedSources.system) {
      silencedPlaybackRef.current = pendingPlaybackRef.current ?? captureCurrentPlayback();
      pendingPlaybackRef.current = null;
      audio?.pause();
      return;
    }

    const nextSource = playbackSourceForMuteState(availablePlaybackSources, mutedSources);
    if (!nextSource) {
      audio?.pause();
      return;
    }
    const silencedRequest = silencedPlaybackRef.current;
    if (silencedRequest) {
      silencedPlaybackRef.current = null;
      if (nextSource === audioSource && (audio?.readyState ?? 0) >= 1) {
        void playLoadedAudio(silencedRequest);
      } else {
        pendingPlaybackRef.current = silencedRequest;
        if (nextSource === audioSource) audio?.load();
        else setAudioSource(nextSource);
      }
      return;
    }

    if (nextSource !== audioSource) {
      pendingPlaybackRef.current = pendingPlaybackRef.current ?? captureCurrentPlayback();
      setAudioSource(nextSource);
    }
  }, [audioSource, availablePlaybackSources, captureCurrentPlayback, mutedSources, playLoadedAudio]);
  useEffect(() => {
    const audio = audioRef.current;
    if (!pendingPlaybackRef.current || !audio) return;
    audio.load();
  }, [audioSource, detail?.id]);
  const handlePlaybackMetadata = useCallback(() => {
    const request = pendingPlaybackRef.current;
    if (!request) return;
    pendingPlaybackRef.current = null;
    void playLoadedAudio(request);
  }, [playLoadedAudio]);
  const seekCitation = useCallback((segmentId: string) => {
    const segment = liveSegments.find((item) => item.id === segmentId);
    if (segment) playSegment(segment.source, segment.startMs);
  }, [liveSegments, playSegment]);

  return (
    <div className="mx-auto flex min-h-[calc(100dvh-3.5rem)] w-full max-w-[1680px] flex-col gap-3 px-3 pb-4 pt-3 sm:px-4 lg:px-5">
      <div className="flex min-h-12 flex-wrap items-center justify-between gap-3 px-1">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h1 className="text-lg font-semibold tracking-[-0.015em] sm:text-xl"><span className="sr-only">Meeting workspace · </span>Meetings</h1>
            {activeMeeting && <Badge variant="outline" className={stateTone(activeMeeting.state)}>{stateLabel(activeMeeting.state)}</Badge>}
          </div>
          <p className="mt-0.5 truncate text-xs text-muted-foreground">Capture, understand, and follow through from one durable workspace.</p>
        </div>
        {!selectedId && <div className="flex flex-wrap items-center justify-end gap-2">
          <input
            ref={meetingImportRef}
            type="file"
            accept="audio/*,video/*,.m4a,.m4v,.mkv,.webm,.opus,.flac,.wav,.mp3,.mp4,.mov,.avi"
            className="sr-only"
            aria-label="Import meeting recording"
            onChange={(event) => {
              const file = event.target.files?.[0];
              event.target.value = "";
              if (!file) return;
              const title = file.name.replace(/\.[^.]+$/, "");
              setMeetingImportCandidate({ file, title, durationSeconds: null });
              setMeetingImportProfileId(profileId || profilesQuery.data?.defaultProfileId || "");
              setMeetingImportProgress({ stage: "Ready", percentage: 0 });
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
            size="sm"
            variant="outline"
            disabled={meetingImportBusy || Boolean(activeMeeting)}
            onClick={() => meetingImportRef.current?.click()}
            className="active:scale-[0.97]"
          >
            {meetingImportBusy
              ? <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />
              : <FileUp className="mr-2 h-3.5 w-3.5" />}
            <span className="hidden sm:inline">Import recording</span>
            <span className="sm:hidden">Import</span>
          </Button>
          <Button type="button" size="sm" onClick={() => document.getElementById("meeting-title")?.focus()} className="active:scale-[0.97]">
            <CirclePlay className="mr-2 h-3.5 w-3.5" />New meeting
          </Button>
        </div>}
      </div>

      {!capabilitiesQuery.isLoading && !capabilitiesQuery.data?.nativeMeetingCapture && (
        <div className="flex items-start gap-3 rounded-2xl border border-amber-300/60 bg-amber-500/10 px-4 py-3 text-sm text-amber-900 dark:text-amber-100" role="status">
          <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0" />
          <div>
            <p className="font-semibold">Native meeting capture is not connected</p>
            <p className="mt-0.5 opacity-80">Meeting recording requires the installed Windows app and its private audio sidecar. History and notes remain available.</p>
          </div>
        </div>
      )}

      <div className="grid min-h-[680px] flex-1 gap-3 min-[1100px]:grid-cols-[248px_minmax(0,1fr)]">
        <aside className={`${selectedId ? "hidden min-[1100px]:block" : ""} rounded-[18px] border border-border/65 bg-card/55 p-2 shadow-sm`}>
          <div className="flex items-center justify-between px-2 py-2">
            <div>
              <p className="text-sm font-semibold">Meetings</p>
              <p className="text-xs text-muted-foreground">{meetingsTotal} saved</p>
            </div>
            <Button type="button" size="icon" variant="outline" onClick={() => setLocation("/meetings")} aria-label="Create meeting">
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
            {meetingsQuery.isError && <p className="px-2 py-5 text-sm text-destructive">Meeting history could not be loaded.</p>}
            {!meetingsQuery.isLoading && !meetingsQuery.isError && meetings.length === 0 && (
              <div className="flex min-w-full items-center gap-3 px-3 py-4 text-left text-sm text-muted-foreground min-[1100px]:block min-[1100px]:py-10 min-[1100px]:text-center">
                <CalendarClock className="h-6 w-6 shrink-0 min-[1100px]:mx-auto min-[1100px]:mb-3 min-[1100px]:h-7 min-[1100px]:w-7" />
                Your first meeting will appear here.
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
                    <span>{formatMoment(meeting.createdAt)}</span>
                    <span className="truncate">{stateLabel(meeting.state)}</span>
                  </div>
                </button>
                <button
                  type="button"
                  aria-label={`Delete ${meeting.title}`}
                  title={OPEN_STATES.has(meeting.state) ? "Stop this meeting before deleting it" : "Delete meeting"}
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
                Load older meetings
              </Button>
            )}
          </div>
        </aside>

        <main className="min-w-0 overflow-hidden rounded-[20px] border border-border/65 bg-card/45 shadow-sm">
          {!selectedId ? (
            <div className="h-full overflow-y-auto">
              <header className="border-b border-border/60 px-5 py-4 sm:px-7">
                <p className="text-[11px] font-semibold uppercase tracking-[0.16em] text-primary">New meeting</p>
                <h2 className="mt-1 text-2xl font-semibold tracking-[-0.025em]">Ready the room</h2>
                <p className="mt-1 max-w-2xl text-sm leading-6 text-muted-foreground">Confirm the title and audio routes. Scriber keeps both sources locally durable while live transcription runs.</p>
              </header>
              <div className="border-b border-border/60 bg-muted/20 px-5 py-3 sm:px-7">
                <div className="grid gap-3 sm:grid-cols-3 sm:gap-4">
                  {[
                    { icon: Mic2, label: "Microphone", detail: "Raw and clean voice" },
                    { icon: Headphones, label: "System audio", detail: "Remote participants" },
                    { icon: Waves, label: "Echo control", detail: "Render-aware AEC3" },
                  ].map(({ icon: Icon, label, detail }) => (
                    <div key={label} className="flex min-w-0 items-center gap-2.5">
                      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-primary/10"><Icon className="h-4 w-4 text-primary" /></span>
                      <div><p className="text-sm font-medium">{label}</p><p className="mt-0.5 text-xs text-muted-foreground">{detail}</p></div>
                    </div>
                  ))}
                </div>
              </div>
              <div className="grid gap-5 p-5 sm:p-7 min-[1100px]:grid-cols-[minmax(0,1fr)_260px]">
                <section className="flex min-w-0 flex-col gap-4">
                {detectionQuery.data?.detection && <div className="rounded-2xl border border-primary/35 bg-primary/5 p-4">
                  <div className="flex items-start gap-3">
                    <MonitorSpeaker className="mt-0.5 h-5 w-5 shrink-0 text-primary" />
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-medium">Meeting activity detected</p>
                      <p className="mt-1 truncate text-sm text-muted-foreground">{detectionQuery.data.detection.label}</p>
                      <div className="mt-3 flex flex-wrap gap-2">
                        <Button type="button" size="sm" onClick={() => setTitle(detectionQuery.data?.detection?.calendarEvent?.subject || detectionQuery.data?.detection?.label || "Meeting")}>Use suggestion</Button>
                        <Button type="button" size="sm" variant="ghost" disabled={detectionDismissMutation.isPending} onClick={() => detectionDismissMutation.mutate(detectionQuery.data!.detection!.detectionId)}>Dismiss</Button>
                      </div>
                    </div>
                  </div>
                </div>}
                {outlookQuery.data?.configured && <div className="rounded-2xl border border-border/70 bg-background/55 p-4">
                  <div className="flex items-start gap-3">
                    <CalendarClock className="mt-0.5 h-5 w-5 shrink-0 text-primary" />
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-medium">Outlook calendar context</p>
                      {outlookQuery.data.connected ? (
                        <>
                          {outlookQuery.data.nextEvent ? <button type="button" className="mt-2 block w-full rounded-xl bg-muted/55 p-3 text-left" onClick={() => setTitle(outlookQuery.data?.nextEvent?.subject ?? "")}>
                            <span className="block truncate text-sm font-medium">{outlookQuery.data.nextEvent.subject || "Untitled Outlook meeting"}</span>
                            <span className="mt-1 block text-xs text-muted-foreground">{formatMoment(outlookQuery.data.nextEvent.start_at)} · Use as meeting title</span>
                          </button> : <p className="mt-1 text-xs text-muted-foreground">Connected · no upcoming event in the synchronized window.</p>}
                          <div className="mt-3 flex gap-2">
                            <Button type="button" size="sm" variant="outline" disabled={outlookMutation.isPending} onClick={() => outlookMutation.mutate("sync")}>Sync</Button>
                            <Button type="button" size="sm" variant="ghost" disabled={outlookMutation.isPending} onClick={() => outlookMutation.mutate("disconnect")}>Disconnect</Button>
                          </div>
                        </>
                      ) : (
                        <div className="mt-2 flex items-center justify-between gap-3">
                          <p className="text-xs leading-5 text-muted-foreground">Optional; requests only User.Read, Calendars.Read and offline_access.</p>
                          <Button type="button" size="sm" variant="outline" disabled={outlookMutation.isPending} onClick={() => outlookMutation.mutate("connect")}>Connect</Button>
                        </div>
                      )}
                    </div>
                  </div>
                </div>}
                <div className="grid min-w-0 gap-4 overflow-hidden rounded-2xl border border-border/70 bg-background/55 p-4">
                  <div className="min-w-0">
                    <label htmlFor="meeting-profile" className="text-sm font-medium">Meeting profile</label>
                    <select id="meeting-profile" value={profileId} onChange={(event) => setProfileId(event.target.value)} className="mt-2 h-10 w-full min-w-0 rounded-lg border border-input bg-background px-3 text-sm">
                      {profilesQuery.data?.profiles.map((profile) => <option key={profile.id} value={profile.id} disabled={!profile.available}>{profile.name}{profile.available ? "" : " (unavailable)"}</option>)}
                    </select>
                    <p className="mt-2 text-xs leading-5 text-muted-foreground">{selectedProfile?.description || "Loading provider capabilities..."}</p>
                    {selectedProfile && <details className="group mt-3 rounded-xl border border-border/60 bg-muted/15">
                      <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-3 py-2.5 text-xs font-medium marker:content-none">
                        <span>How this profile works</span>
                        <ChevronDown className="h-3.5 w-3.5 text-muted-foreground group-open:rotate-180 motion-reduce:transform-none" />
                      </summary>
                      <div className="divide-y divide-border/60 border-t border-border/60 px-3">
                        {(selectedProfile.stages ?? []).map((stage) => <div key={stage.id} className="grid gap-1 py-2.5 sm:grid-cols-[130px_minmax(0,1fr)]">
                          <p className="text-[11px] font-medium text-muted-foreground">{stage.label}</p>
                          <div className="min-w-0">
                            <p className="truncate text-xs font-semibold">{stage.provider} · <span className="font-mono font-normal">{stage.model}</span></p>
                            <p className="mt-0.5 text-[11px] leading-4 text-muted-foreground">{stage.purpose}</p>
                          </div>
                        </div>)}
                      </div>
                    </details>}
                    {selectedProfile && !selectedProfile.available && <p className="mt-2 text-xs text-amber-700 dark:text-amber-300">{selectedProfile.unavailableReason}</p>}
                  </div>
                  <div className="min-w-0 border-t border-border/60 pt-4">
                    <div className="flex items-center justify-between gap-3">
                      <p className="text-sm font-medium">Audio routes</p>
                      <Button type="button" size="sm" variant="ghost" disabled={audioDevicesQuery.isFetching} onClick={() => void audioDevicesQuery.refetch()}><RefreshCw className={`mr-2 h-3.5 w-3.5 ${audioDevicesQuery.isFetching ? "animate-spin" : ""}`} />Refresh</Button>
                    </div>
                    <div className="mt-2 grid gap-2 sm:grid-cols-2">
                      <div className="min-w-0"><label htmlFor="meeting-microphone" className="text-xs text-muted-foreground">Microphone</label><select id="meeting-microphone" value={microphoneEndpointHash} onChange={(event) => setMicrophoneEndpointHash(event.target.value)} className="mt-1 h-9 w-full min-w-0 rounded-lg border border-input bg-background px-2 text-xs"><option value="">Windows default microphone</option>{audioDevicesQuery.data?.capture.map((endpoint) => <option key={endpoint.endpointIdHash} value={endpoint.endpointIdHash}>{endpoint.friendlyName}{endpoint.isDefault ? " (default)" : ""}</option>)}</select></div>
                      <div className="min-w-0"><label htmlFor="meeting-render" className="text-xs text-muted-foreground">System playback</label><select id="meeting-render" value={renderEndpointHash} onChange={(event) => setRenderEndpointHash(event.target.value)} className="mt-1 h-9 w-full min-w-0 rounded-lg border border-input bg-background px-2 text-xs"><option value="">Windows default playback device</option>{audioDevicesQuery.data?.render.map((endpoint) => <option key={endpoint.endpointIdHash} value={endpoint.endpointIdHash}>{endpoint.friendlyName}{endpoint.isDefault ? " (default)" : ""}</option>)}</select></div>
                    </div>
                    <p className="mt-2 text-xs leading-5 text-muted-foreground">{audioDevicesQuery.data?.available ? `${audioDevicesQuery.data.capture.length} microphones and ${audioDevicesQuery.data.render.length} playback routes available.` : "Native audio inventory is unavailable."}</p>
                    <Button type="button" size="sm" variant="outline" className="mt-3 h-auto min-h-9 w-full whitespace-normal px-3 text-center leading-5" disabled={!audioDevicesQuery.data?.available || deviceTestMutation.isPending || capabilitiesQuery.data?.liveMicBusy || Boolean(capabilitiesQuery.data?.activeMeeting)} onClick={() => deviceTestMutation.mutate()}>
                      {deviceTestMutation.isPending ? <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" /> : <Waves className="mr-2 h-3.5 w-3.5" />}Test microphone and playback
                    </Button>
                    <p className="mt-2 text-[11px] leading-4 text-muted-foreground">Speak normally during the 3-second test. Scriber also plays a short tone to verify speaker loopback. Nothing is saved or sent to a provider.</p>
                    {deviceTestMutation.data && <div className="mt-3 space-y-2" role="status">
                      {(["microphone", "system"] as const).map((source) => {
                        const result = deviceTestMutation.data?.sources[source];
                        const rms = result?.rms ?? 0;
                        const levelPercent = rms > 0
                          ? Math.max(3, Math.min(100, ((20 * Math.log10(rms) + 60) / 60) * 100))
                          : 0;
                        const hasFrames = (result?.frames ?? 0) > 0 && !result?.errorCode;
                        return <div key={source} className="rounded-lg border border-border/60 bg-muted/35 px-2.5 py-2">
                          <div className="flex items-center justify-between gap-3 text-[11px]"><span className="font-medium">{source === "microphone" ? "Microphone input" : "Speaker loopback"}</span><span className={hasFrames ? "text-emerald-700 dark:text-emerald-300" : "text-amber-700 dark:text-amber-300"}>{hasFrames ? "Signal received" : result?.errorCode ? "Could not read" : "No signal"}</span></div>
                          <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-muted"><div className="h-full rounded-full bg-primary" style={{ width: `${levelPercent}%` }} /></div>
                          <p className="mt-1 text-[10px] text-muted-foreground">{result?.frames ?? 0} frames · {rms > 0 ? `${(20 * Math.log10(rms)).toFixed(1)} dBFS` : "silence"}</p>
                        </div>;
                      })}
                      <p className="flex items-center gap-1.5 text-[11px] text-muted-foreground"><Volume2 className="h-3.5 w-3.5" />{deviceTestMutation.data.testTonePlayed ? "Test tone played" : "Test tone unavailable"} · AEC3 {deviceTestMutation.data.aecActive ? "initialized" : "not active"}</p>
                    </div>}
                  </div>
                </div>
                <div className="order-first">
                  <label htmlFor="meeting-title" className="text-sm font-medium">Meeting title</label>
                  <Input id="meeting-title" value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Weekly product sync" className="mt-2 h-12 text-base" />
                </div>
                </section>
                <aside className="h-fit min-w-0 rounded-2xl border border-border/70 bg-background/45 p-4 min-[1100px]:sticky min-[1100px]:top-4">
                  <div className="flex items-center gap-2">
                    <ShieldCheck className={`h-4 w-4 ${longSessionReady ? "text-emerald-600 dark:text-emerald-300" : "text-amber-600 dark:text-amber-300"}`} />
                    <h3 className="text-sm font-semibold">{longSessionReady ? "Ready for up to 5 hours" : "Long-session readiness"}</h3>
                  </div>
                  <p className="mt-1.5 text-xs leading-5 text-muted-foreground">
                    {longSessionReady
                      ? "Local capture stays protected every 30 seconds. Final processing continues safely after you stop."
                      : !capabilitiesQuery.data?.nativeMeetingCapture
                        ? "Native Windows capture must be available before a long meeting can start."
                        : longSession?.availableFreeBytes == null
                          ? "Free-space verification is unavailable here. Keep at least 6 GB free before a five-hour session."
                          : !longSession.storageReady
                            ? "Free space is below the conservative reserve for a five-hour capture and finalization."
                            : !finalProviderCapability?.fiveHourSupported
                              ? finalProviderCapability?.fiveHourReason || "The selected final transcription model is not verified for a five-hour source."
                              : "Five-hour readiness could not be verified."}
                  </p>
                  <div className="mt-4 divide-y divide-border/60 rounded-xl border border-border/60 bg-muted/20 px-3">
                    <div className="flex items-center justify-between gap-3 py-2.5 text-xs"><span className="text-muted-foreground">Native capture</span><span className={capabilitiesQuery.data?.nativeMeetingCapture ? "text-emerald-600 dark:text-emerald-300" : "text-amber-700 dark:text-amber-300"}>{capabilitiesQuery.data?.nativeMeetingCapture ? "Ready" : "Unavailable"}</span></div>
                    <div className="flex items-center justify-between gap-3 py-2.5 text-xs"><span className="text-muted-foreground">Recovery</span><span>Every {longSession?.checkpointIntervalSeconds ?? 30} s</span></div>
                    <div className="flex items-center justify-between gap-3 py-2.5 text-xs"><span className="text-muted-foreground">Free space</span><span className={longSession?.storageReady ? "text-emerald-600 dark:text-emerald-300" : "text-amber-700 dark:text-amber-300"}>{longSession?.availableFreeBytes != null ? formatImportBytes(longSession.availableFreeBytes) : "Not verified"}</span></div>
                    <div className="flex items-center justify-between gap-3 py-2.5 text-xs"><span className="text-muted-foreground">Estimated capacity</span><span>{formatCapacity(longSession?.estimatedCaptureSeconds)}</span></div>
                    <div className="flex items-center justify-between gap-3 py-2.5 text-xs"><span className="text-muted-foreground">Final STT</span><span className={finalProviderCapability?.fiveHourSupported ? "text-emerald-600 dark:text-emerald-300" : "text-amber-700 dark:text-amber-300"}>{finalProviderDurationLabel}</span></div>
                    <div className="flex items-center justify-between gap-3 py-2.5 text-xs"><span className="text-muted-foreground">Speaker labels</span><span className={finalProviderCapability?.batchDiarization ? "text-emerald-600 dark:text-emerald-300" : "text-amber-700 dark:text-amber-300"}>{finalProviderCapability?.batchDiarization ? "Native provider" : "Best effort"}</span></div>
                  </div>
                  {!finalProviderCapability?.batchDiarization && <p className="mt-2 text-[11px] leading-4 text-muted-foreground">For speaker labels beyond 60 minutes, choose a final model with native diarization.</p>}
                <details className="group mt-3 rounded-xl border border-border/60 bg-muted/15">
                  <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-3 py-2.5 text-xs font-medium marker:content-none">
                    <span>Advanced · Retention &amp; Voice Library</span>
                    <ChevronDown className="h-3.5 w-3.5 text-muted-foreground group-open:rotate-180 motion-reduce:transform-none" />
                  </summary>
                  <div className="border-t border-border/60 p-3">
                  <div className="flex flex-col gap-2 border-b border-border/60 pb-3">
                    <label htmlFor="meeting-retention" className="text-xs font-medium">Local audio retention</label>
                    <select id="meeting-retention" value={audioRetentionDays} onChange={(event) => setAudioRetentionDays(Number(event.target.value))} className="h-9 rounded-lg border border-input bg-background px-3 text-xs">
                      <option value={0}>Until deleted</option>
                      <option value={7}>7 days</option>
                      <option value={30}>30 days</option>
                      <option value={90}>90 days</option>
                    </select>
                    <p className="text-[11px] leading-4 text-muted-foreground">Transcript, notes and outputs remain after audio is purged.</p>
                  </div>
                  <div className="mt-3 flex items-start justify-between gap-4">
                    <div>
                      <p className="text-sm font-medium">Local Voice Library</p>
                      <p className="mt-1 text-xs leading-5 text-muted-foreground">Optional biometric speaker matching using a local checksum-verified WeSpeaker ONNX model. Nothing is sent to exports, chat, providers, or support bundles.</p>
                    </div>
                    <input
                      type="checkbox"
                      checked={voiceLibraryEnabled}
                      disabled={!speakerModelQuery.data?.optedIn || !speakerModelQuery.data?.installed}
                      onChange={(event) => setVoiceLibraryEnabled(event.target.checked)}
                      className="mt-1 h-4 w-4 accent-primary"
                      aria-label="Enable Voice Library for this meeting"
                    />
                  </div>
                  {speakerModelQuery.data?.optedIn && !speakerModelQuery.data.installed && <Button type="button" size="sm" variant="outline" className="mt-3" disabled={speakerModelMutation.isPending} onClick={() => speakerModelMutation.mutate()}>
                    {speakerModelMutation.isPending ? <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" /> : <Download className="mr-2 h-3.5 w-3.5" />}Install optional model
                  </Button>}
                  {!speakerModelQuery.data?.optedIn && <p className="mt-2 text-xs text-amber-700 dark:text-amber-300">First confirm the biometric-processing opt-in in Settings.</p>}
                  {(speakerProfilesQuery.data?.items.length ?? 0) >= 2 && <div className="mt-4 border-t border-border/60 pt-4">
                    <p className="text-xs font-medium">Correct profile matches</p>
                    <div className="mt-2 grid gap-2">
                      <select aria-label="Profile to keep" value={mergeTargetProfileId} onChange={(event) => setMergeTargetProfileId(event.target.value)} className="h-9 rounded-lg border border-input bg-background px-2 text-xs">
                        <option value="">Keep profile…</option>
                        {speakerProfilesQuery.data?.items.map((profile) => <option key={profile.id} value={profile.id}>{profile.displayName} · {profile.sampleCount}</option>)}
                      </select>
                      <select aria-label="Profile to merge" value={mergeSourceProfileId} onChange={(event) => setMergeSourceProfileId(event.target.value)} className="h-9 rounded-lg border border-input bg-background px-2 text-xs">
                        <option value="">Merge profile…</option>
                        {speakerProfilesQuery.data?.items.filter((profile) => profile.id !== mergeTargetProfileId).map((profile) => <option key={profile.id} value={profile.id}>{profile.displayName} · {profile.sampleCount}</option>)}
                      </select>
                      <Button type="button" size="sm" variant="outline" disabled={!mergeTargetProfileId || !mergeSourceProfileId || mergeTargetProfileId === mergeSourceProfileId || mergeProfilesMutation.isPending} onClick={() => mergeProfilesMutation.mutate()}>Merge</Button>
                    </div>
                    <p className="mt-2 text-[11px] leading-4 text-muted-foreground">The kept profile is recomputed deterministically from its bounded local observations.</p>
                  </div>}
                  </div>
                </details>
                <div className="mt-4 flex flex-col gap-2">
                  <Button
                    type="button"
                    size="lg"
                    disabled={startBlocked || startMutation.isPending}
                    onClick={() => startMutation.mutate()}
                    className="h-11 w-full active:scale-[0.97]"
                  >
                    {startMutation.isPending ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <CirclePlay className="mr-2 h-4 w-4" />}
                    Start meeting
                  </Button>
                  {activeMeeting && <p className="text-sm text-muted-foreground">Finish “{activeMeeting.title}” first.</p>}
                </div>
                </aside>
              </div>
            </div>
          ) : detailQuery.isLoading ? (
            <div className="space-y-4"><div className="h-12 animate-pulse rounded-xl bg-muted" /><div className="h-96 animate-pulse rounded-2xl bg-muted/70" /></div>
          ) : detailQuery.isError || !detail ? (
            <div className="flex h-full items-center justify-center text-center"><div><AlertTriangle className="mx-auto mb-3 h-8 w-8 text-destructive" /><p className="font-medium">Meeting could not be loaded.</p></div></div>
          ) : (
            <div className="flex h-full min-h-0 flex-col">
              <header className="flex flex-col gap-4 border-b border-border/60 px-5 py-4 sm:flex-row sm:items-center sm:justify-between sm:px-6">
                <div className="min-w-0">
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    className="mb-2 -ml-2 h-7 px-2 text-xs min-[1100px]:hidden"
                    onClick={() => setLocation("/meetings")}
                  >
                    <ChevronLeft className="mr-1 h-3.5 w-3.5" />All meetings
                  </Button>
                  <div className="flex flex-wrap items-center gap-2">
                    <h2 className="truncate text-xl font-semibold tracking-[-0.02em]">{detail.title}</h2>
                    <Badge variant="outline" className={stateTone(detail.state)}>{stateLabel(detail.state)}</Badge>
                  </div>
                  <p className="mt-1 text-xs text-muted-foreground">Started {formatMoment(detail.startedAt || detail.createdAt)}</p>
                </div>
                {(detail.state === "recording" || detail.state === "paused") && <MeetingElapsedTime startedAt={detail.startedAt} audioGaps={detail.audioGaps} paused={detail.state === "paused"} pausedAtTimelineMs={detail.captureMetadata.pauseStartedAtMs} pausedAtUtc={detail.captureMetadata.pauseStartedAtUtc} recordingTimelineOffsetMs={detail.captureMetadata.timelineOffsetMs} recordingTimelineStartedAtUtc={detail.captureMetadata.timelineStartedAtUtc} finalProviderMaxDurationSeconds={detailFinalProviderCapability?.maxDurationSeconds} />}
                <div className="flex flex-wrap gap-2">
                  {detail.state === "ready" && <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button type="button" variant="outline" className="h-9 active:scale-[0.97]">
                        <Download className="mr-2 h-3.5 w-3.5" />Export<ChevronDown className="ml-2 h-3.5 w-3.5 text-muted-foreground" />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end" className="min-w-52">
                      {(["json", "md", "pdf", "docx"] as const).map((format) => (
                        <DropdownMenuItem
                          key={format}
                          aria-label={`Export meeting as ${format.toUpperCase()}`}
                          disabled={exportMutation.isPending}
                          onSelect={() => {
                            exportMutation.mutate({
                              path: `/api/meetings/${detail.id}/export/${format}`,
                              fallbackName: `${detail.title}.${format}`,
                            });
                          }}
                        >
                          <FileText className="mr-2 h-3.5 w-3.5" />{format.toUpperCase()}
                        </DropdownMenuItem>
                      ))}
                      <DropdownMenuSeparator />
                      <DropdownMenuItem
                        onSelect={() => setEmailDialogOpen(true)}
                      >
                        <Mail className="mr-2 h-3.5 w-3.5" />Create email draft
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>}
                  {detail.state === "recording" && <Button variant="outline" disabled={controlMutation.isPending} onClick={() => controlMutation.mutate({ id: detail.id, action: "pause" })}><CirclePause className="mr-2 h-4 w-4" />Pause</Button>}
                  {detail.state === "paused" && <Button variant="outline" disabled={controlMutation.isPending} onClick={() => controlMutation.mutate({ id: detail.id, action: "resume" })}><CirclePlay className="mr-2 h-4 w-4" />Resume</Button>}
                  {(detail.state === "recording" || detail.state === "paused") && <Button variant="destructive" disabled={controlMutation.isPending} onClick={() => controlMutation.mutate({ id: detail.id, action: "stop" })}><Square className="mr-2 h-4 w-4" />Stop</Button>}
                  {OPEN_STATES.has(detail.state) && controlMutation.isPending && <Loader2 className="h-5 w-5 animate-spin self-center text-muted-foreground" />}
                </div>
              </header>

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
                    {latestCheckpoint && <span>{latestCheckpoint.segmentCount} final segment{latestCheckpoint.segmentCount === 1 ? "" : "s"}</span>}
                  </div>
                  <div className="mt-2 space-y-2">{(["microphone", "system"] as const).map((source) => liveStatuses[source] && (
                    <div key={`${source}-${liveStatuses[source]!.status}`} role="status" className={`rounded-lg border px-3 py-2 text-xs ${liveStatuses[source]!.status === "recovered" ? "border-emerald-300/60 bg-emerald-500/10 text-emerald-900 dark:text-emerald-100" : "border-amber-300/60 bg-amber-500/10 text-amber-900 dark:text-amber-100"}`}>
                      {source === "microphone" ? "Microphone" : "System audio"} live transcription {liveStatuses[source]!.status === "reconnecting" ? "lost its provider connection and is reconnecting. Durable audio recording continues." : liveStatuses[source]!.status === "degraded" ? "preview is falling behind and may omit captions. Durable audio recording continues without dropping frames." : "reconnected. Final transcription will recover from the local audio."}{liveStatuses[source]!.reconnectCount > 0 ? ` Attempt ${liveStatuses[source]!.reconnectCount}.` : ""}
                    </div>
                  ))}</div>
                </div>
              )}

              {(["stopping", "finalizing", "analyzing"] as MeetingState[]).includes(detail.state) && <div className="mt-4 rounded-xl border border-border/60 bg-muted/35 px-4 py-3" role="status">
                <div className="flex items-center justify-between gap-3 text-xs"><span className="font-medium">{meetingProgress?.status || (detail.state === "analyzing" ? "Building cited meeting intelligence" : "Finalizing saved audio")}</span><span className="tabular-nums text-muted-foreground">{Math.round((meetingProgress?.progress ?? 0) * 100)}%</span></div>
                <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-muted" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round((meetingProgress?.progress ?? 0) * 100)}><div className="h-full origin-left rounded-full bg-primary transition-transform duration-200 motion-reduce:transition-none" style={{ transform: `scaleX(${meetingProgress?.progress ?? 0})` }} /></div>
              </div>}

              {detail.errorMessage && (
                <div className="mt-4 rounded-xl border border-amber-300/50 bg-amber-500/10 px-4 py-3 text-sm text-amber-900 dark:text-amber-100">
                  <p>{detail.errorMessage}</p>
                  {detail.state === "finalization_failed" && (
                    <label className="mt-3 block max-w-sm text-xs font-semibold">
                      Final transcription route
                      <select
                        value={retryFinalProvider}
                        onChange={(event) => setRetryFinalProvider(event.target.value)}
                        className="mt-1.5 h-9 w-full rounded-lg border border-border bg-background px-3 text-sm font-normal text-foreground outline-none focus:ring-2 focus:ring-primary"
                      >
                        {(profilesQuery.data?.finalProviderOptions ?? []).map((option) => (
                          <option key={option.id} value={option.id} disabled={option.available === false}>
                            {option.label} · {option.model}{option.available === false ? " · not configured" : ""}
                          </option>
                        ))}
                      </select>
                      <span className="mt-1 block font-normal opacity-80">
                        Choose another configured route when this recording exceeds the previous provider limit.
                      </span>
                    </label>
                  )}
                  {["capture_failed", "finalization_failed", "analysis_failed", "interrupted"].includes(detail.state) && (
                    <div className="mt-3 flex flex-wrap gap-2">
                      {detail.state === "interrupted" && (
                        <Button type="button" size="sm" disabled={controlMutation.isPending} onClick={() => controlMutation.mutate({ id: detail.id, action: "resume" })}>
                          <CirclePlay className="mr-2 h-3.5 w-3.5" />Resume capture
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
                        {detail.state === "analysis_failed" ? "Retry analysis" : "Finalize saved audio"}
                      </Button>
                      <Button type="button" size="sm" variant="ghost" disabled={recoveryMutation.isPending} onClick={() => recoveryMutation.mutate({ id: detail.id, action: "discard" })}>Discard</Button>
                    </div>
                  )}
                </div>
              )}

              {outputsStale && (
                <div className="mx-5 mt-3 flex flex-col gap-3 rounded-xl border border-amber-300/60 bg-amber-500/10 px-4 py-3 text-sm text-amber-950 dark:text-amber-100 sm:mx-6 sm:flex-row sm:items-center sm:justify-between" role="status">
                  <div className="min-w-0">
                    <p className="font-semibold">Transcript corrected after this brief was generated</p>
                    <p className="mt-0.5 text-xs opacity-80">Playback, search, and new exports use the correction. Regenerate the brief when its wording depends on the edited passage.</p>
                  </div>
                  {detail.state === "ready" && (
                    <Button type="button" size="sm" variant="outline" className="shrink-0 active:scale-[0.97]" disabled={analysisMutation.isPending || !hasCanonicalTranscript} onClick={generateAnalysis}>
                      {analysisMutation.isPending && <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />}Regenerate brief
                    </Button>
                  )}
                </div>
              )}

              <MeetingWorkspaceTabs value={workspaceView} onChange={setWorkspaceView} />
              {detail.segments.length > 0 && hasPlayableAudio && (
                <div className="mx-5 mt-3 flex flex-col gap-2 rounded-xl bg-muted/45 px-3 py-2 sm:mx-6 sm:flex-row sm:flex-wrap sm:items-center">
                  <div className="flex items-center gap-1" aria-label="Playback track controls">
                    {(["microphone", "system"] as const).map((source) => {
                      const muted = mutedSources[source];
                      const available = availablePlaybackSources.has(source);
                      return <button type="button" key={source} disabled={!available} aria-pressed={available && !muted} onClick={() => togglePlaybackSource(source)} className={`rounded-md px-2 py-1 text-[11px] font-medium disabled:cursor-not-allowed disabled:opacity-40 ${!muted && available ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted"}`}>{source === "microphone" ? "Mic" : "System"} {!available ? "unavailable" : muted ? "muted" : "on"}</button>;
                    })}
                  </div>
                  <audio
                    ref={audioRef}
                    controls
                    preload="metadata"
                    src={apiUrl(audioSource === "mix" ? `/api/meetings/${detail.id}/audio` : `/api/meetings/${detail.id}/audio/${audioSource}`)}
                    onLoadedMetadata={handlePlaybackMetadata}
                    onPlay={() => setPlaybackError("")}
                    onError={() => setPlaybackError("The saved meeting audio could not be loaded.")}
                    className="h-8 w-full sm:ml-auto sm:max-w-md"
                  />
                  {playbackError && <p className="text-xs text-destructive sm:basis-full" role="alert">{playbackError}</p>}
                </div>
              )}
              {detail.segments.length > 0 && !hasPlayableAudio && (
                <div className="mx-5 mt-3 flex items-center gap-2 rounded-xl border border-border/60 bg-muted/35 px-3 py-2 text-xs text-muted-foreground sm:mx-6" role="status">
                  <Headphones className="h-3.5 w-3.5" />
                  {detail.captureMetadata.audioPurgedAt
                    ? "Audio is no longer retained. The transcript and meeting outputs remain available."
                    : "No playable audio asset is available for this meeting."}
                </div>
              )}

              <div className="grid min-h-0 flex-1 2xl:grid-cols-[minmax(0,1fr)_300px]">
                <section className="min-w-0 px-5 py-5 sm:px-6">
                  {workspaceView !== "transcript" ? (
                    <div className="max-w-3xl">
                      {workspaceView === "chat" ? (
                        <div className="space-y-4">
                          <div><h3 className="text-sm font-semibold">Ask this meeting</h3><p className="mt-1 text-xs text-muted-foreground">Answers are grounded in the canonical transcript and retain segment citations.</p></div>
                          {chatAnswer && <div className="rounded-2xl bg-muted/55 p-4"><p className="whitespace-pre-wrap text-sm leading-7">{chatAnswer.content}</p>{chatAnswer.citations.length > 0 && <div className="mt-3 flex flex-wrap gap-1.5">{chatAnswer.citations.map((citation) => <button type="button" key={citation} onClick={() => seekCitation(citation)}><Badge variant="outline" className="font-mono text-[10px] hover:border-primary">{citation.slice(0, 8)}</Badge></button>)}</div>}</div>}
                          <Textarea value={chatQuestion} onChange={(event) => setChatQuestion(event.target.value)} placeholder="What did we decide about the launch?" rows={3} />
                          <Button disabled={!chatQuestion.trim() || chatMutation.isPending} onClick={() => chatMutation.mutate({ id: detail.id, question: chatQuestion })}>{chatMutation.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}Ask meeting</Button>
                        </div>
                      ) : workspaceView === "notes" ? (
                        <div className="max-w-2xl"><h3 className="text-sm font-semibold">Meeting notes</h3><p className="mt-1 text-xs text-muted-foreground">Your notes remain separate from generated outputs.</p><Textarea value={note} onChange={(event) => setNote(event.target.value)} placeholder="Capture decisions and follow-ups..." rows={8} className="mt-4" />{detail.notes.filter((savedNote) => savedNote.id !== "workspace").map((savedNote) => <div key={savedNote.id} className="mt-3 rounded-xl bg-muted/55 p-3"><p className="text-xs font-medium text-primary">{formatOffset(savedNote.atMs)}</p><p className="mt-1 text-sm leading-6">{savedNote.body}</p></div>)}</div>
                      ) : !analysis ? (
                        <div className="flex min-h-64 items-center justify-center rounded-2xl border border-dashed border-border text-center text-sm text-muted-foreground">
                          <div>{detail.state === "analyzing" ? <Loader2 className="mx-auto mb-3 h-6 w-6 animate-spin" /> : <AlertTriangle className="mx-auto mb-3 h-6 w-6" />}<p>{detail.state === "analyzing" ? "Building cited meeting intelligence…" : "Structured analysis is not available yet."}</p>{detail.state === "ready" && <Button type="button" size="sm" variant="outline" className="mt-4" disabled={analysisMutation.isPending || !hasCanonicalTranscript} onClick={generateAnalysis}>{analysisMutation.isPending && <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />}Generate analysis</Button>}</div>
                        </div>
                      ) : workspaceView === "overview" ? (
                        <div className="max-w-4xl">
                          <div className="flex items-center justify-between gap-3">
                            <div><p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-primary">Meeting brief</p><h3 className="mt-1 text-lg font-semibold tracking-tight">What matters now</h3></div>
                            {detail.state === "ready" && <Button type="button" size="sm" variant="outline" disabled={analysisMutation.isPending || !hasCanonicalTranscript} onClick={generateAnalysis}>{analysisMutation.isPending && <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />}Regenerate</Button>}
                          </div>
                          <section className="mt-5 border-l-2 border-primary pl-4">
                            <div className="flex items-center gap-2"><Sparkles className="h-4 w-4 text-primary" /><h4 className="text-sm font-semibold">Key outcome</h4></div>
                            <p className="mt-2 whitespace-pre-wrap text-sm leading-7 text-foreground/90">{String(analysis.executiveSummary || "No summary was produced.")}</p>
                          </section>
                          <div className="mt-7 grid gap-7">
                            <section><div className="flex items-center gap-2"><Check className="h-4 w-4 text-primary" /><h4 className="text-sm font-semibold">Decisions</h4></div><EvidenceList items={analysis.decisions} onCitation={seekCitation} /></section>
                            <section><div className="flex items-center gap-2"><AlertTriangle className="h-4 w-4 text-primary" /><h4 className="text-sm font-semibold">Risks and open questions</h4></div><EvidenceList items={[...(Array.isArray(analysis.risks) ? analysis.risks : []), ...(Array.isArray(analysis.openQuestions) ? analysis.openQuestions : [])]} onCitation={seekCitation} /></section>
                          </div>
                          <section className="mt-7 border-t border-border/60 pt-6"><div className="flex items-center gap-2"><FileText className="h-4 w-4 text-primary" /><h4 className="text-sm font-semibold">Action items</h4></div><ActionItems items={detail.actionItems ?? []} saving={actionItemMutation.isPending} onCitation={seekCitation} onChange={(item, changes) => actionItemMutation.mutate({ id: detail.id, itemId: item.id, changes })} /></section>
                        </div>
                      ) : workspaceView === "decisions" ? <EvidenceList items={analysis.decisions} onCitation={seekCitation} />
                        : workspaceView === "actions" ? <ActionItems items={detail.actionItems ?? []} saving={actionItemMutation.isPending} onCitation={seekCitation} onChange={(item, changes) => actionItemMutation.mutate({ id: detail.id, itemId: item.id, changes })} />
                          : <EvidenceList items={analysis.openQuestions} onCitation={seekCitation} />}
                    </div>
                  ) : <>
                  <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
                    <div className="flex items-center gap-2"><Users className="h-4 w-4 text-muted-foreground" /><h3 className="text-sm font-semibold">Transcript</h3></div>
                    <span className="text-xs text-muted-foreground">{transcriptSearch.trim() ? `${visibleTranscriptSegments.length} of ${groupedSegments.length} segments` : `${groupedSegments.length} segments`}</span>
                  </div>
                  <label className="relative mb-4 block max-w-xl">
                    <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
                    <Input
                      type="search"
                      value={transcriptSearch}
                      onChange={(event) => setTranscriptSearch(event.target.value)}
                      placeholder="Search transcript, speaker, or phrase"
                      className="h-9 pl-9 text-sm"
                      aria-label="Search this meeting transcript"
                    />
                  </label>
                  {detail.speakers.length > 0 && <div className="mb-4 flex flex-wrap gap-2">
                    {detail.speakers.map((speaker) => <label key={speaker.id} className="flex items-center gap-2 rounded-full border border-border/70 bg-muted/35 px-3 py-1.5 text-xs">
                      <span className="text-muted-foreground">{speaker.sourceHint === "microphone" ? "Mic" : "Remote"}</span>
                      <input
                        defaultValue={speaker.displayName || speaker.label}
                        aria-label={`Rename ${speaker.displayName || speaker.label}`}
                        className="w-24 bg-transparent font-medium outline-none"
                        onBlur={(event) => {
                          const displayName = event.target.value.trim();
                          if (displayName && displayName !== speaker.displayName) {
                            speakerMutation.mutate({ id: detail.id, speakerId: speaker.id, displayName });
                          }
                        }}
                      />
                      {speaker.profileId && <button type="button" className="rounded px-1 text-[10px] text-muted-foreground hover:bg-background hover:text-foreground" disabled={splitSpeakerMutation.isPending} onClick={(event) => { event.preventDefault(); splitSpeakerMutation.mutate({ id: detail.id, speakerId: speaker.id }); }} title="Separate this speaker from the matched Voice Library profile">Separate</button>}
                    </label>)}
                  </div>}
                  <div>
                    {groupedSegments.length === 0 ? (
                      <div className="flex min-h-64 items-center justify-center rounded-2xl border border-dashed border-border text-center text-sm text-muted-foreground">
                        <div><Waves className="mx-auto mb-3 h-7 w-7" /><p>{OPEN_STATES.has(detail.state) ? "Listening for speech…" : "No transcript segments are available."}</p></div>
                      </div>
                    ) : visibleTranscriptSegments.length === 0 ? (
                      <div className="flex min-h-48 items-center justify-center rounded-2xl border border-dashed border-border text-center text-sm text-muted-foreground">
                        <div><Search className="mx-auto mb-3 h-6 w-6" /><p>No transcript segment matches “{transcriptSearch.trim()}”.</p></div>
                      </div>
                    ) : <VirtualMeetingTranscript
                      segments={visibleTranscriptSegments}
                      search={transcriptSearch}
                      hasPlayableAudio={hasPlayableAudio}
                      onPlay={playSegment}
                      canEdit={detail.state === "ready" && visibleTranscriptSegments.every((segment) => segment.revision === "canonical")}
                      savingSegmentId={segmentEditMutation.isPending ? segmentEditMutation.variables?.segment.id ?? "" : ""}
                      onSave={(segment, text) => segmentEditMutation.mutate({ segment, action: "edit", text })}
                      onUndo={(segment) => segmentEditMutation.mutate({ segment, action: "undo" })}
                    />}
                  </div>
                  </>}
                </section>

                <aside className="border-t border-border/60 bg-muted/15 px-5 py-5 lg:border-l lg:border-t-0">
                  <div className="flex items-center gap-2"><NotebookPen className="h-4 w-4 text-muted-foreground" /><h3 className="text-sm font-semibold">Live notes</h3></div>
                  <div className="mt-3 space-y-2">
                    <Textarea value={note} onChange={(event) => { const body = event.target.value; setNote(body); noteDraftRef.current = { meetingId: selectedId, body: body.trim(), savedBody: noteDraftRef.current.savedBody }; }} placeholder="Capture decisions and follow-ups…" rows={5} />
                    <p className="flex items-center text-xs text-muted-foreground">
                      {noteMutation.isPending ? <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" /> : <Check className="mr-2 h-3.5 w-3.5" />}
                      {noteMutation.isPending ? "Saving…" : "Notes autosave and AI regeneration never overwrites them."}
                    </p>
                  </div>
                  <div className="mt-5 space-y-2">
                    {detail.notes.length === 0 && <p className="text-xs leading-5 text-muted-foreground">Notes are timestamped and retained with this meeting.</p>}
                    {detail.notes.filter((savedNote) => savedNote.id !== "workspace").map((savedNote) => <div key={savedNote.id} className="rounded-xl bg-muted/60 px-3 py-2.5"><p className="text-xs font-medium text-primary">{formatOffset(savedNote.atMs)}</p><p className="mt-1 text-sm leading-5">{savedNote.body}</p></div>)}
                  </div>
                  <details className="group mt-5 rounded-xl border border-border/60 bg-background/35">
                    <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-3 py-2.5 text-xs font-semibold marker:content-none">
                      <span>Technical details</span>
                      <ChevronDown className="h-3.5 w-3.5 text-muted-foreground group-open:rotate-180 motion-reduce:transform-none" />
                    </summary>
                    <div className="border-t border-border/60 p-3">
                      <div className="flex items-center gap-2"><Sparkles className="h-4 w-4 text-primary" /><h3 className="text-xs font-semibold">Models used</h3></div>
                      <dl className="mt-3 space-y-2 text-[11px]">
                        <div className="flex items-start justify-between gap-3"><dt className="text-muted-foreground">Live transcript</dt><dd className="text-right font-mono">{detail.origin === "imported" ? "Not used (imported)" : detailLiveModel || detail.liveProvider}</dd></div>
                        <div className="flex items-start justify-between gap-3"><dt className="text-muted-foreground">Final transcript</dt><dd className="text-right font-mono">{detail.finalRoute?.model || detail.finalProvider}</dd></div>
                        <div className="flex items-start justify-between gap-3"><dt className="text-muted-foreground">Summary and actions</dt><dd className="break-all text-right font-mono">{detail.analysisModel}</dd></div>
                      </dl>
                      {detail.state === "ready" && aecMetrics && <div className="mt-4 border-t border-border/60 pt-3">
                        <div className="flex items-center justify-between gap-3">
                          <span className="text-xs font-medium">Render-active attenuation</span>
                          <span className={`font-mono text-sm font-semibold tabular-nums ${typeof aecMetrics.echoReductionDb === "number" && aecMetrics.echoReductionDb > 0 ? "text-emerald-600 dark:text-emerald-300" : "text-muted-foreground"}`}>
                            {typeof aecMetrics.echoReductionDb === "number" ? `${aecMetrics.echoReductionDb.toFixed(1)} dB` : "Not measured"}
                          </span>
                        </div>
                        <p className="mt-1 text-[11px] leading-4 text-muted-foreground">Raw-to-clean microphone energy while system audio was active · {Math.round((aecMetrics.renderActiveDurationMs ?? 0) / 1000)}s measured.</p>
                      </div>}
                    </div>
                  </details>
                  {detail.state === "ready" && <details className="group mt-6 border-t border-border/60 pt-3">
                    <summary className="flex cursor-pointer list-none items-center justify-between gap-3 py-2 text-sm font-semibold marker:content-none">
                      <span>Delivery & integrations</span>
                      <ChevronDown className="h-4 w-4 text-muted-foreground group-open:rotate-180 motion-reduce:transform-none" />
                    </summary>
                    <div className="pt-2">
                    <p className="text-xs leading-5 text-muted-foreground">HTTPS only. Redirects are blocked, and the signing secret is never stored.</p>
                    <div className="mt-3 space-y-2">
                      <div><label htmlFor="meeting-webhook-url" className="text-xs text-muted-foreground">Destination URL</label><Input id="meeting-webhook-url" value={webhookUrl} onChange={(event) => { setWebhookUrl(event.target.value); setWebhookPreview(null); setWebhookConfirmed(false); }} placeholder="https://automation.example/meeting" className="mt-1 h-9 text-xs" /></div>
                      <div><label htmlFor="meeting-webhook-secret" className="text-xs text-muted-foreground">Optional HMAC secret</label><Input id="meeting-webhook-secret" type="password" autoComplete="off" value={webhookSecret} onChange={(event) => setWebhookSecret(event.target.value)} className="mt-1 h-9 text-xs" /></div>
                      <Button type="button" size="sm" variant="outline" disabled={!webhookUrl.trim() || webhookPreviewMutation.isPending} onClick={() => webhookPreviewMutation.mutate({ id: detail.id, url: webhookUrl.trim() })}>{webhookPreviewMutation.isPending && <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />}Preview payload</Button>
                    </div>
                    {webhookPreview && <div className="mt-3 rounded-xl border border-border/70 bg-muted/35 p-3 text-xs">
                      <p className="font-medium">{webhookPreview.payload.event || "meeting.ready"}</p>
                      <p className="mt-1 break-all text-muted-foreground">{webhookPreview.target}</p>
                      <dl className="mt-3 grid grid-cols-3 gap-2"><div><dt className="text-muted-foreground">Size</dt><dd className="mt-0.5 font-medium">{webhookPreview.byteSize} B</dd></div><div><dt className="text-muted-foreground">Segments</dt><dd className="mt-0.5 font-medium">{webhookPreview.payload.segments?.length ?? 0}</dd></div><div><dt className="text-muted-foreground">Notes</dt><dd className="mt-0.5 font-medium">{webhookPreview.payload.notes?.length ?? 0}</dd></div></dl>
                      <label className="mt-3 flex cursor-pointer items-start gap-2"><input type="checkbox" checked={webhookConfirmed} onChange={(event) => setWebhookConfirmed(event.target.checked)} className="mt-0.5 h-4 w-4 accent-primary" /><span>I reviewed this target and payload.</span></label>
                      <Button type="button" size="sm" className="mt-3" disabled={!webhookConfirmed || !webhookPreview || webhookDeliveryMutation.isPending} onClick={() => webhookPreview && webhookDeliveryMutation.mutate({ id: detail.id, url: webhookUrl, secret: webhookSecret, previewHash: webhookPreview.previewHash, confirmed: webhookConfirmed })}>{webhookDeliveryMutation.isPending && <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />}Send webhook</Button>
                    </div>}
                    {(deliveriesQuery.data?.items.length ?? 0) > 0 && <div className="mt-3 space-y-2">{deliveriesQuery.data?.items.slice(0, 3).map((delivery) => <div key={delivery.id} className="rounded-lg bg-muted/45 px-3 py-2 text-xs"><div className="flex items-center justify-between gap-2"><span className="truncate text-muted-foreground">{delivery.target}</span><Badge variant="outline">{delivery.status}</Badge></div><p className="mt-1 text-muted-foreground">{delivery.attemptCount} attempt{delivery.attemptCount === 1 ? "" : "s"}</p></div>)}</div>}
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
            <DialogTitle>Import a meeting recording</DialogTitle>
            <DialogDescription>
              Create a durable meeting workspace, then run final transcription, speaker separation, analysis, search, and playback on one shared timeline.
            </DialogDescription>
          </DialogHeader>
          {meetingImportCandidate && <div className="space-y-4">
            <div className="rounded-xl border border-border/65 bg-muted/30 p-4">
              <div className="flex min-w-0 items-start gap-3">
                <div className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-primary/10 text-primary"><FileUp className="h-4 w-4" /></div>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium">{meetingImportCandidate.file.name}</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {formatImportDuration(meetingImportCandidate.durationSeconds)} · {formatImportBytes(meetingImportCandidate.file.size)}
                  </p>
                </div>
              </div>
            </div>
            <div>
              <label htmlFor="meeting-import-title" className="text-xs font-medium text-muted-foreground">Meeting title</label>
              <Input
                id="meeting-import-title"
                value={meetingImportCandidate.title}
                disabled={meetingImportBusy}
                onChange={(event) => setMeetingImportCandidate((current) => current ? { ...current, title: event.target.value } : current)}
                className="mt-1.5 h-10"
              />
            </div>
            <div>
              <label htmlFor="meeting-import-profile" className="text-xs font-medium text-muted-foreground">Transcription profile</label>
              <select
                id="meeting-import-profile"
                value={meetingImportProfile?.id || ""}
                disabled={meetingImportBusy}
                onChange={(event) => setMeetingImportProfileId(event.target.value)}
                className="mt-1.5 h-10 w-full rounded-md border border-input bg-background px-3 text-sm outline-none transition-colors focus:border-ring focus:ring-2 focus:ring-ring/20"
              >
                {(profilesQuery.data?.profiles ?? []).map((item) => <option key={item.id} value={item.id} disabled={!item.available}>{item.name}{item.available ? "" : " · unavailable"}</option>)}
              </select>
              {meetingImportProfile && <div className="mt-2 rounded-lg border border-border/55 px-3 py-2.5 text-xs leading-5">
                <div className="flex items-center justify-between gap-3"><span className="text-muted-foreground">Final STT</span><span className="font-medium">{meetingImportProfile.stages.find((stage) => stage.id === "final")?.model || meetingImportProfile.finalProvider}</span></div>
                <div className="flex items-center justify-between gap-3"><span className="text-muted-foreground">Maximum duration</span><span className="font-medium">{meetingImportFinalProviderCapability?.maxDurationSeconds != null ? formatImportDuration(meetingImportFinalProviderCapability.maxDurationSeconds) : "No published duration limit"}</span></div>
                <div className="flex items-center justify-between gap-3"><span className="text-muted-foreground">Speakers</span><span className="text-right font-medium">{profilesQuery.data?.providerCapabilities[meetingImportProfile.finalProvider]?.batchDiarization ? "Native when verified · local fallback otherwise" : "Local Sherpa-ONNX fallback"}</span></div>
                <div className="flex items-center justify-between gap-3"><span className="text-muted-foreground">Language</span><span className="font-medium">{meetingImportProfile.language || "Auto"}</span></div>
              </div>}
              {meetingImportExceedsProviderDuration && <div className="mt-2 rounded-lg border border-amber-300/60 bg-amber-500/10 px-3 py-2.5 text-xs leading-5 text-amber-900 dark:text-amber-100" role="alert">
                This recording exceeds the selected final STT limit. Choose a compatible model in Settings before importing it.
              </div>}
            </div>
            {meetingImportBusy && <div className="rounded-xl border border-border/65 bg-muted/25 px-4 py-3" role="status">
              <div className="flex items-center justify-between gap-3 text-xs"><span className="font-medium">{meetingImportProgress.stage}</span><span className="tabular-nums text-muted-foreground">{meetingImportProgress.percentage}%</span></div>
              <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-muted" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={meetingImportProgress.percentage}><div className="h-full origin-left rounded-full bg-primary transition-transform duration-200 motion-reduce:transition-none" style={{ transform: `scaleX(${meetingImportProgress.percentage / 100})` }} /></div>
              <p className="mt-2 text-xs text-muted-foreground">The source is copied into Scriber before final processing. Closing Scriber later will not discard an accepted import.</p>
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
                  setMeetingImportProgress({ stage: "Cancel requested", percentage: 0 });
                } else setMeetingImportCandidate(null);
              }}
            >
              {meetingImportBusy ? "Cancel import" : "Cancel"}
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
              {meetingImportBusy ? "Importing…" : "Import recording"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={emailDialogOpen} onOpenChange={setEmailDialogOpen}>
        <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-[680px]">
          <DialogHeader>
            <DialogTitle>Share meeting by email</DialogTitle>
            <DialogDescription>Create a populated email in your default mail app, or download an Outlook-compatible draft with the meeting document attached.</DialogDescription>
          </DialogHeader>
          {emailPreviewQuery.isLoading ? (
            <div className="grid gap-3 py-3"><div className="h-12 animate-pulse rounded-xl bg-muted" /><div className="h-40 animate-pulse rounded-xl bg-muted" /></div>
          ) : emailPreviewQuery.isError || !emailPreviewQuery.data ? (
            <p className="rounded-xl border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">The email template could not be prepared.</p>
          ) : (
            <div className="space-y-4">
              <div className="grid gap-3 rounded-xl border border-border/70 bg-muted/25 p-3 text-sm">
                <div><p className="text-[11px] font-semibold text-muted-foreground">To</p><p className="mt-1 break-words">{emailPreviewQuery.data.recipients.length > 0 ? emailPreviewQuery.data.recipients.map((item) => item.name ? `${item.name} <${item.address}>` : item.address).join(", ") : "No Outlook participants were stored. Add recipients in your mail app."}</p></div>
                <div><p className="text-[11px] font-semibold text-muted-foreground">Subject</p><p className="mt-1 font-medium">{emailPreviewQuery.data.subject}</p></div>
              </div>
              <div>
                <p className="text-xs font-semibold">Email body preview</p>
                <pre className="mt-2 max-h-52 overflow-y-auto whitespace-pre-wrap rounded-xl border border-border/70 bg-background p-3 font-sans text-xs leading-5 text-foreground/85">{emailPreviewQuery.data.body}</pre>
              </div>
              <fieldset>
                <legend className="text-xs font-semibold">Draft attachment</legend>
                <div className="mt-2 grid gap-2 sm:grid-cols-4">
                  {([['', 'Body only'], ['pdf', 'PDF'], ['docx', 'Word'], ['md', 'Markdown']] as const).map(([value, label]) => (
                    <label key={value || "body"} className={`flex cursor-pointer items-center gap-2 rounded-lg border px-3 py-2 text-xs ${emailAttachment === value ? "border-primary bg-primary/5 text-foreground" : "border-border/70 text-muted-foreground hover:bg-muted/50"}`}>
                      <input type="radio" name="meeting-email-attachment" value={value} checked={emailAttachment === value} onChange={() => setEmailAttachment(value)} className="accent-primary" />
                      {value ? <Paperclip className="h-3.5 w-3.5" /> : <Mail className="h-3.5 w-3.5" />}{label}
                    </label>
                  ))}
                </div>
              </fieldset>
              <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
                <Button type="button" variant="outline" onClick={() => void composeEmailBody()}>
                  <Mail className="mr-2 h-4 w-4" />Open email with summary
                </Button>
                <Button
                  type="button"
                  disabled={exportMutation.isPending}
                  onClick={() => exportMutation.mutate({
                    path: `/api/meetings/${detail?.id}/export-email${emailAttachment ? `?attachment=${emailAttachment}` : ""}`,
                    fallbackName: `${detail?.title || "Meeting"} - email draft.eml`,
                  })}
                >
                  {exportMutation.isPending ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Paperclip className="mr-2 h-4 w-4" />}
                  Download email draft{emailAttachment ? ` + ${emailAttachment.toUpperCase()}` : ""}
                </Button>
              </div>
            </div>
          )}
        </DialogContent>
      </Dialog>

      <AlertDialog open={Boolean(meetingPendingDelete)} onOpenChange={(open) => !open && setMeetingPendingDelete(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete this meeting?</AlertDialogTitle>
            <AlertDialogDescription>
              “{meetingPendingDelete?.title}” will be removed permanently, including its transcript, generated outputs, notes, and locally retained audio. This cannot be undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteMeetingMutation.isPending}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              disabled={!meetingPendingDelete || deleteMeetingMutation.isPending}
              onClick={(event) => {
                event.preventDefault();
                if (meetingPendingDelete) deleteMeetingMutation.mutate(meetingPendingDelete.id);
              }}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {deleteMeetingMutation.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              Delete meeting
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

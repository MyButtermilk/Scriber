import { useCallback, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocation } from "wouter";
import { CirclePause, CirclePlay, Headphones, Mic2, Square } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useSharedWebSocket, type ScriberWebSocketMessage } from "@/contexts/WebSocketContext";
import { useI18n } from "@/i18n";
import { apiUrl } from "@/lib/backend";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";
import { apiRequest } from "@/lib/queryClient";
import {
  ACTIVE_MEETING_QUERY_PATH,
  applyMeetingSummaryEvent,
  isActiveMeetingState,
  isMeetingWebSocketReconnect,
  refreshActiveMeeting,
  refreshMeetingDetail,
} from "@/lib/meeting-cache";
import { meetingControlVisibility, meetingTimerNowMs } from "@/lib/meeting-controls";
import { calculateMeetingElapsedMs, formatMeetingOffset } from "@/lib/meeting-playback";
import type { MeetingState, MeetingsResponse } from "@/lib/api-types";

const VISIBLE_STATES = new Set<MeetingState>([
  "starting", "recording", "paused", "stopping", "finalizing", "analyzing",
]);

export function ActiveMeetingPill() {
  const { t } = useI18n();
  const [, setLocation] = useLocation();
  const queryClient = useQueryClient();
  const [now, setNow] = useState(() => Date.now());
  const lastFrameAtRef = useRef({ microphone: 0, system: 0 });
  const previousMeetingIdRef = useRef<string | null>(null);
  const previousMeetingStateRef = useRef<MeetingState | null>(null);
  const wsHasConnectedRef = useRef(false);
  const wsWasConnectedRef = useRef(false);
  const [liveIssue, setLiveIssue] = useState<"" | "reconnecting" | "degraded">("");
  const meetingsQuery = useQuery<MeetingsResponse>({
    queryKey: ["/api/meetings"],
    queryFn: async ({ signal }) => {
      const response = await fetchWithTimeout(
        apiUrl(ACTIVE_MEETING_QUERY_PATH),
        { credentials: "include", signal },
        15_000,
      );
      if (!response.ok) throw new Error(`Meeting status failed (${response.status})`);
      return response.json() as Promise<MeetingsResponse>;
    },
    staleTime: 10_000,
  });
  const meeting = meetingsQuery.data?.activeMeeting;

  useEffect(() => {
    const meetingId = meeting?.id ?? null;
    const meetingState = meeting?.state ?? null;
    const meetingChanged = previousMeetingIdRef.current !== meetingId;
    const stateChanged = previousMeetingStateRef.current !== meetingState;
    const enteredRecording = meetingState === "recording"
      && previousMeetingStateRef.current !== "recording";
    const initial = Date.now();
    if (stateChanged) setNow(initial);
    if (meetingChanged || enteredRecording) {
      lastFrameAtRef.current = { microphone: initial, system: initial };
      setLiveIssue("");
    }
    if (!meetingId) {
      lastFrameAtRef.current = { microphone: 0, system: 0 };
    }
    previousMeetingIdRef.current = meetingId;
    previousMeetingStateRef.current = meetingState;
  }, [meeting?.id, meeting?.state]);

  useEffect(() => {
    if (meeting?.state !== "recording") return;
    const initial = Date.now();
    setNow(initial);
    const handle = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(handle);
  }, [meeting?.id, meeting?.state]);

  const handleMessage = useCallback((message: ScriberWebSocketMessage) => {
    if (message.type === "meeting_state") {
      applyMeetingSummaryEvent(queryClient, message.meeting);
      if (!isActiveMeetingState(message.meeting.state)) {
        void refreshMeetingDetail(queryClient, message.meeting.id);
        setLiveIssue("");
      }
      return;
    }
    if (!meeting || !("meetingId" in message) || message.meetingId !== meeting.id) return;
    if (message.type === "meeting_audio_level" && (message.source === "microphone" || message.source === "system")) {
      lastFrameAtRef.current[message.source] = Date.now();
    } else if (message.type === "meeting_live_status") {
      setLiveIssue(message.status === "recovered" ? "" : message.status);
    }
  }, [meeting, queryClient]);
  const { isConnected } = useSharedWebSocket(handleMessage);

  useEffect(() => {
    if (isMeetingWebSocketReconnect(
      wsHasConnectedRef.current,
      wsWasConnectedRef.current,
      isConnected,
    )) {
      // The websocket handshake intentionally has no Meeting snapshot. Re-read
      // only the compact active-Meeting response after a genuine reconnect;
      // the initial connection already has the query bootstrap above.
      void refreshActiveMeeting(queryClient);
    }
    if (isConnected) wsHasConnectedRef.current = true;
    wsWasConnectedRef.current = isConnected;
  }, [isConnected, queryClient]);

  const controlMutation = useMutation({
    mutationFn: async (action: "pause" | "resume" | "stop") => {
      if (!meeting) throw new Error("No active Meeting is available");
      const response = await apiRequest("POST", `/api/meetings/${meeting.id}/${action}`);
      return response.json() as Promise<MeetingsResponse["items"][number]>;
    },
    onSuccess: (updated, action) => {
      applyMeetingSummaryEvent(queryClient, updated);
      if (action === "stop") void refreshMeetingDetail(queryClient, updated.id);
    },
  });

  if (!meeting || !VISIBLE_STATES.has(meeting.state)) return null;
  const staleSource = meeting.state === "recording" && (
    now - lastFrameAtRef.current.microphone > 5_000
    || now - lastFrameAtRef.current.system > 5_000
  );
  const processing = ["stopping", "finalizing", "analyzing"].includes(meeting.state);
  const needsAttention = meeting.state === "recording" && (staleSource || Boolean(liveIssue));
  const status = meeting.state === "starting"
    ? t("Starting audio capture…")
    : processing
    ? meeting.state === "analyzing" ? t("Building meeting brief") : t("Finalizing durable audio")
    : meeting.state === "paused" ? t("Capture paused")
    : needsAttention ? t("Capture needs attention")
    : t("Mic and system audio are recording");
  const controls = meetingControlVisibility(meeting.state);
  const captureMetadata = meeting.captureMetadata ?? {};
  const elapsedNow = meetingTimerNowMs(meeting.state, meeting.endedAt, now);
  const elapsedMs = calculateMeetingElapsedMs(
    meeting.startedAt,
    elapsedNow,
    [],
    captureMetadata.pauseStartedAtMs,
    captureMetadata.pauseStartedAtUtc,
    captureMetadata.timelineOffsetMs,
    captureMetadata.timelineStartedAtUtc,
  );

  return (
    <div
      className="border-b border-border/55 bg-sidebar px-3 py-2"
      role="region"
      aria-label={t("Active meeting")}
      data-testid="active-meeting-pill"
      data-state={meeting.state}
      data-meeting-id={meeting.id}
    >
      <div className={`mx-auto flex min-h-11 max-w-4xl items-center gap-3 rounded-xl border px-3 py-2 shadow-sm ${needsAttention ? "border-amber-300/70 bg-amber-500/10" : "border-border/70 bg-card/90"}`}>
        <button
          type="button"
          className="flex min-w-0 flex-1 items-center gap-3 rounded-lg text-left outline-none focus-visible:ring-2 focus-visible:ring-primary active:scale-[0.99]"
          onClick={() => setLocation(`/meetings/${meeting.id}`)}
          aria-label={t("Open active meeting {{title}}", { title: meeting.title })}
        >
          <span className={`h-2.5 w-2.5 shrink-0 rounded-full ${needsAttention ? "bg-amber-500" : meeting.state === "starting" || meeting.state === "paused" || processing ? "bg-primary" : "bg-red-500"}`} aria-hidden="true" />
          <span className="min-w-0 flex-1">
            <span className="block truncate text-sm font-semibold">{meeting.title}</span>
            <span className="mt-0.5 flex items-center gap-2 truncate text-[11px] text-muted-foreground" aria-live="polite">
              <span>{status}</span>
              {!processing && meeting.state !== "starting" && <><Mic2 className="h-3 w-3" aria-hidden="true" /><Headphones className="h-3 w-3" aria-hidden="true" /></>}
            </span>
          </span>
          <span
            className="shrink-0 font-mono text-sm font-semibold tabular-nums"
            data-testid="active-meeting-elapsed"
            data-elapsed-ms={elapsedMs}
          >
            {formatMeetingOffset(elapsedMs)}
          </span>
        </button>
        {(controls.pause || controls.resume || controls.stop) && (
          <div className="flex shrink-0 items-center gap-1">
            {controls.pause && (
              <Button type="button" size="icon" variant="ghost" className="h-9 w-9 active:scale-[0.97]" disabled={controlMutation.isPending} onClick={() => controlMutation.mutate("pause")} aria-label={t("Pause active meeting")} data-testid="active-meeting-pause">
                <CirclePause className="h-4 w-4" />
              </Button>
            )}
            {controls.resume && (
              <Button type="button" size="icon" variant="ghost" className="h-9 w-9 active:scale-[0.97]" disabled={controlMutation.isPending} onClick={() => controlMutation.mutate("resume")} aria-label={t("Resume active meeting")} data-testid="active-meeting-resume">
                <CirclePlay className="h-4 w-4" />
              </Button>
            )}
            {controls.stop && (
              <Button type="button" size="icon" variant="ghost" className="h-9 w-9 text-destructive hover:text-destructive active:scale-[0.97]" disabled={controlMutation.isPending} onClick={() => controlMutation.mutate("stop")} aria-label={t("Stop active meeting")} data-testid="active-meeting-stop">
                <Square className="h-4 w-4" />
              </Button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

import { useCallback, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocation } from "wouter";
import { CirclePause, CirclePlay, Headphones, Mic2, Square } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useSharedWebSocket, type ScriberWebSocketMessage } from "@/contexts/WebSocketContext";
import { apiUrl } from "@/lib/backend";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";
import { apiRequest } from "@/lib/queryClient";
import { applyMeetingSummaryEvent } from "@/lib/meeting-cache";
import type { MeetingState, MeetingsResponse } from "@/lib/api-types";

const VISIBLE_STATES = new Set<MeetingState>([
  "starting", "recording", "paused", "stopping", "finalizing", "analyzing",
]);

function elapsedLabel(startedAt: string | null, now: number): string {
  if (!startedAt) return "0:00";
  const seconds = Math.max(0, Math.floor((now - new Date(startedAt).getTime()) / 1_000));
  const hours = Math.floor(seconds / 3_600);
  const minutes = Math.floor((seconds % 3_600) / 60);
  const remainder = seconds % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`
    : `${minutes}:${String(remainder).padStart(2, "0")}`;
}

export function ActiveMeetingPill() {
  const [, setLocation] = useLocation();
  const queryClient = useQueryClient();
  const [now, setNow] = useState(() => Date.now());
  const lastFrameAtRef = useRef({ microphone: 0, system: 0 });
  const [liveIssue, setLiveIssue] = useState<"" | "reconnecting" | "degraded">("");
  const meetingsQuery = useQuery<MeetingsResponse>({
    queryKey: ["/api/meetings"],
    queryFn: async ({ signal }) => {
      const response = await fetchWithTimeout(
        apiUrl("/api/meetings?limit=100"),
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
    if (!meeting || !VISIBLE_STATES.has(meeting.state)) return;
    const initial = Date.now();
    if (!lastFrameAtRef.current.microphone) lastFrameAtRef.current.microphone = initial;
    if (!lastFrameAtRef.current.system) lastFrameAtRef.current.system = initial;
    const handle = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(handle);
  }, [meeting]);

  const handleMessage = useCallback((message: ScriberWebSocketMessage) => {
    if (message.type === "meeting_state") {
      applyMeetingSummaryEvent(queryClient, message.meeting);
      if (!VISIBLE_STATES.has(message.meeting.state)) setLiveIssue("");
      return;
    }
    if (!meeting || !("meetingId" in message) || message.meetingId !== meeting.id) return;
    if (message.type === "meeting_audio_level" && (message.source === "microphone" || message.source === "system")) {
      lastFrameAtRef.current[message.source] = Date.now();
    } else if (message.type === "meeting_live_status") {
      setLiveIssue(message.status === "recovered" ? "" : message.status);
    }
  }, [meeting, queryClient]);
  useSharedWebSocket(handleMessage);

  const controlMutation = useMutation({
    mutationFn: async (action: "pause" | "resume" | "stop") => {
      if (!meeting) return;
      const response = await apiRequest("POST", `/api/meetings/${meeting.id}/${action}`);
      const updated = await response.json();
      applyMeetingSummaryEvent(queryClient, updated);
    },
  });

  if (!meeting || !VISIBLE_STATES.has(meeting.state)) return null;
  const staleSource = meeting.state === "recording" && (
    now - lastFrameAtRef.current.microphone > 5_000
    || now - lastFrameAtRef.current.system > 5_000
  );
  const processing = ["stopping", "finalizing", "analyzing"].includes(meeting.state);
  const needsAttention = staleSource || Boolean(liveIssue);
  const status = processing
    ? meeting.state === "analyzing" ? "Building meeting brief" : "Finalizing durable audio"
    : meeting.state === "paused" ? "Capture paused"
    : needsAttention ? "Capture needs attention"
    : "Mic and system audio are recording";

  return (
    <div className="border-b border-border/55 bg-sidebar px-3 py-2" role="region" aria-label="Active meeting">
      <div className={`mx-auto flex min-h-11 max-w-4xl items-center gap-3 rounded-xl border px-3 py-2 shadow-sm ${needsAttention ? "border-amber-300/70 bg-amber-500/10" : "border-border/70 bg-card/90"}`}>
        <button
          type="button"
          className="flex min-w-0 flex-1 items-center gap-3 rounded-lg text-left outline-none focus-visible:ring-2 focus-visible:ring-primary active:scale-[0.99]"
          onClick={() => setLocation(`/meetings/${meeting.id}`)}
          aria-label={`Open active meeting ${meeting.title}`}
        >
          <span className={`h-2.5 w-2.5 shrink-0 rounded-full ${needsAttention ? "bg-amber-500" : meeting.state === "paused" || processing ? "bg-primary" : "bg-red-500"}`} aria-hidden="true" />
          <span className="min-w-0 flex-1">
            <span className="block truncate text-sm font-semibold">{meeting.title}</span>
            <span className="mt-0.5 flex items-center gap-2 truncate text-[11px] text-muted-foreground" aria-live="polite">
              <span>{status}</span>
              {!processing && <><Mic2 className="h-3 w-3" aria-hidden="true" /><Headphones className="h-3 w-3" aria-hidden="true" /></>}
            </span>
          </span>
          <span className="shrink-0 font-mono text-sm font-semibold tabular-nums">{elapsedLabel(meeting.startedAt, now)}</span>
        </button>
        {!processing && (
          <div className="flex shrink-0 items-center gap-1">
            {meeting.state === "recording" ? (
              <Button type="button" size="icon" variant="ghost" className="h-9 w-9 active:scale-[0.97]" disabled={controlMutation.isPending} onClick={() => controlMutation.mutate("pause")} aria-label="Pause active meeting">
                <CirclePause className="h-4 w-4" />
              </Button>
            ) : (
              <Button type="button" size="icon" variant="ghost" className="h-9 w-9 active:scale-[0.97]" disabled={controlMutation.isPending} onClick={() => controlMutation.mutate("resume")} aria-label="Resume active meeting">
                <CirclePlay className="h-4 w-4" />
              </Button>
            )}
            <Button type="button" size="icon" variant="ghost" className="h-9 w-9 text-destructive hover:text-destructive active:scale-[0.97]" disabled={controlMutation.isPending} onClick={() => controlMutation.mutate("stop")} aria-label="Stop active meeting">
              <Square className="h-4 w-4" />
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}

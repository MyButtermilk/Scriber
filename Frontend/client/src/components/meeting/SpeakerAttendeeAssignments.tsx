import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  ChevronDown,
  Loader2,
  Mail,
  ShieldCheck,
  Sparkles,
  UserCheck,
  Users,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { apiUrl } from "@/lib/backend";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";
import { apiRequest } from "@/lib/queryClient";
import type {
  MeetingSpeakerAssignment,
  MeetingSpeakerAssignmentsResponse,
  MeetingSpeakerSuggestion,
  OutlookCalendarContact,
  OutlookCalendarEvent,
} from "@/lib/api-types";

const SUGGESTION_PRIORITY: Record<MeetingSpeakerSuggestion["source"], number> = {
  voice_profile: 0,
  account: 1,
  llm: 2,
};

function contactId(contact: OutlookCalendarContact): string {
  return contact.participantId || contact.address.trim().toLocaleLowerCase();
}

function contactIdentityKeys(contact: OutlookCalendarContact): string[] {
  return [contact.address, ...(contact.aliases ?? []), contact.participantId ?? ""]
    .map((value) => value.trim().toLocaleLowerCase())
    .filter(Boolean);
}

async function fetchAssignments(meetingId: string, signal?: AbortSignal): Promise<MeetingSpeakerAssignmentsResponse> {
  const response = await fetchWithTimeout(apiUrl(`/api/meetings/${meetingId}/speaker-assignments`), {
    credentials: "include",
    signal,
  }, 15_000);
  const payload = await response.json().catch(() => ({})) as MeetingSpeakerAssignmentsResponse & { message?: string };
  if (!response.ok) throw new Error(payload.message || `Speaker assignments could not be loaded (${response.status})`);
  return payload;
}

function attendeeOptions(event: OutlookCalendarEvent): OutlookCalendarContact[] {
  const seen = new Set<string>();
  return [event.organizer, ...event.participants, event.currentUser ?? null].filter((contact): contact is OutlookCalendarContact => {
    if (!contact || contact.type === "resource") return false;
    const keys = contactIdentityKeys(contact);
    if (keys.length === 0 || keys.some((key) => seen.has(key))) return false;
    keys.forEach((key) => seen.add(key));
    return true;
  });
}

function attendeeIdForContact(attendees: OutlookCalendarContact[], contact: OutlookCalendarContact | null): string {
  if (!contact) return "";
  const keys = new Set(contactIdentityKeys(contact));
  const attendee = attendees.find((candidate) => (
    contactIdentityKeys(candidate).some((key) => keys.has(key))
  ));
  return attendee ? contactId(attendee) : "";
}

function bestSuggestion(item: MeetingSpeakerAssignment): MeetingSpeakerSuggestion | null {
  return [...item.suggestions].sort((left, right) => {
    const sourceDifference = SUGGESTION_PRIORITY[left.source] - SUGGESTION_PRIORITY[right.source];
    if (sourceDifference !== 0) return sourceDifference;
    return (right.confidence ?? -1) - (left.confidence ?? -1);
  })[0] ?? null;
}

function suggestionLabel(source: MeetingSpeakerSuggestion["source"]): string {
  if (source === "voice_profile") return "Voice match · unconfirmed";
  if (source === "account") return "Your Outlook account · unconfirmed";
  return "AI suggestion · unconfirmed";
}

function attendeeQualifier(attendee: OutlookCalendarContact): string {
  const labels: string[] = [];
  if (attendee.isCurrentUser) labels.push("you");
  if (attendee.type === "optional") labels.push("optional");
  if (attendee.response === "declined") labels.push("declined invitation");
  return labels.length > 0 ? ` (${labels.join(", ")})` : "";
}

function confidenceLabel(confidence: number | null): string {
  if (confidence == null || !Number.isFinite(confidence)) return "";
  return `${Math.round(Math.max(0, Math.min(1, confidence)) * 100)}% match`;
}

function suggestionForAttendee(item: MeetingSpeakerAssignment, participantId: string): MeetingSpeakerSuggestion | null {
  return item.suggestions
    .filter((suggestion) => contactId(suggestion.attendee) === participantId)
    .sort((left, right) => SUGGESTION_PRIORITY[left.source] - SUGGESTION_PRIORITY[right.source])[0] ?? null;
}

export function SpeakerAttendeeAssignments({
  meetingId,
  calendarEvent,
  onAssignmentsChanged,
}: {
  meetingId: string;
  calendarEvent: OutlookCalendarEvent | null;
  onAssignmentsChanged: () => void;
}) {
  const queryClient = useQueryClient();
  const [selectedParticipantIds, setSelectedParticipantIds] = useState<Record<string, string>>({});
  const [panelOpen, setPanelOpen] = useState(true);
  const assignmentsQuery = useQuery<MeetingSpeakerAssignmentsResponse>({
    queryKey: ["/api/meetings", meetingId, "speaker-assignments"],
    queryFn: ({ signal }) => fetchAssignments(meetingId, signal),
    enabled: Boolean(meetingId && calendarEvent),
    staleTime: 10_000,
  });
  const event = assignmentsQuery.data?.calendarEvent ?? calendarEvent;
  const attendees = useMemo(() => event ? attendeeOptions(event) : [], [event]);
  const unresolvedCount = assignmentsQuery.data?.items.filter((item) => !item.confirmedAttendee).length ?? 0;

  useEffect(() => {
    const items = assignmentsQuery.data?.items ?? [];
    setSelectedParticipantIds((current) => {
      const next: Record<string, string> = {};
      for (const item of items) {
        const currentId = current[item.speakerId];
        const stillAvailable = currentId && attendees.some((attendee) => contactId(attendee) === currentId);
        next[item.speakerId] = stillAvailable
          ? currentId
          : attendeeIdForContact(attendees, item.confirmedAttendee)
            || (bestSuggestion(item) ? contactId(bestSuggestion(item)!.attendee) : "")
            || "";
      }
      return next;
    });
  }, [assignmentsQuery.data?.items, attendees]);

  const suggestMutation = useMutation({
    mutationFn: async () => {
      const response = await apiRequest("POST", `/api/meetings/${meetingId}/speaker-assignments/suggest`);
      return response.json() as Promise<MeetingSpeakerAssignmentsResponse>;
    },
    onSuccess: (payload) => {
      queryClient.setQueryData(["/api/meetings", meetingId, "speaker-assignments"], payload);
    },
  });

  const confirmMutation = useMutation({
    mutationFn: async ({ speakerId, participantId, suggestionSource }: { speakerId: string; participantId: string | null; suggestionSource: MeetingSpeakerSuggestion["source"] | "manual" }) => {
      const response = await apiRequest("PATCH", `/api/meetings/${meetingId}/speakers/${speakerId}/attendee`, {
        participantId,
        confirmed: true,
        suggestionSource,
      });
      return response.json();
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["/api/meetings", meetingId, "speaker-assignments"] });
      void queryClient.invalidateQueries({ queryKey: ["/api/meetings", meetingId] });
      void queryClient.invalidateQueries({ queryKey: ["/api/meetings", meetingId, "email-preview"] });
      onAssignmentsChanged();
    },
  });

  if (!calendarEvent) return null;

  return (
    <details
      className="group mx-5 mt-3 overflow-hidden rounded-xl border border-border/65 bg-muted/20 sm:mx-6"
      open={panelOpen}
      onToggle={(event) => setPanelOpen(event.currentTarget.open)}
    >
      <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-3 marker:content-none">
        <div className="flex min-w-0 items-center gap-3">
          <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-primary/10 text-primary"><Users className="h-4 w-4" /></span>
          <div className="min-w-0">
            <p className="text-sm font-semibold">Match speakers to participants</p>
            <p className="mt-0.5 truncate text-xs text-muted-foreground">
              {assignmentsQuery.isLoading
                ? "Checking saved voices and Outlook participants…"
                : unresolvedCount > 0
                  ? `${unresolvedCount} ${unresolvedCount === 1 ? "speaker needs" : "speakers need"} your confirmation`
                  : "All detected speaker names have been reviewed"}
            </p>
          </div>
        </div>
        <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground transition-transform duration-[var(--duration-quick)] group-open:rotate-180 motion-reduce:transition-none" />
      </summary>

      <div className="border-t border-border/60 p-4">
        {assignmentsQuery.isLoading ? (
          <div className="grid gap-2" role="status" aria-label="Loading speaker assignments">
            {[0, 1].map((item) => <div key={item} className="h-24 animate-pulse rounded-xl bg-muted motion-reduce:animate-none" />)}
          </div>
        ) : assignmentsQuery.isError ? (
          <div className="rounded-xl border border-destructive/30 bg-destructive/10 p-3" role="alert">
            <p className="text-sm font-medium text-destructive">Speaker suggestions could not be loaded.</p>
            <p className="mt-1 text-xs text-muted-foreground">The transcript and Outlook event remain saved.</p>
            <Button type="button" size="sm" variant="outline" className="mt-3" onClick={() => void assignmentsQuery.refetch()}>Try again</Button>
          </div>
        ) : (assignmentsQuery.data?.items.length ?? 0) === 0 ? (
          <p className="rounded-xl border border-dashed border-border/70 px-4 py-6 text-center text-sm text-muted-foreground">No separate speakers were detected in this transcript.</p>
        ) : (
          <div className="space-y-3">
            <div className="flex flex-col gap-3 rounded-xl border border-border/60 bg-background/45 p-3 sm:flex-row sm:items-start sm:justify-between">
              <div className="flex min-w-0 items-start gap-2.5">
                <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
                <div>
                  <p className="text-xs font-semibold">Saved voice and account matches run first on this device.</p>
                  <p className="mt-1 text-[11px] leading-4 text-muted-foreground">
                    AI suggestions are optional. If you request them, Scriber sends short transcript excerpts and participant names to {assignmentsQuery.data?.llmModel || "your configured summary model"}. Outlook email addresses are not sent. Every suggestion stays unconfirmed until you approve it.
                  </p>
                </div>
              </div>
              {unresolvedCount > 0 && assignmentsQuery.data?.llmSuggestionAvailable && (
                <Button type="button" size="sm" variant="outline" className="shrink-0" disabled={suggestMutation.isPending} onClick={() => suggestMutation.mutate()}>
                  {suggestMutation.isPending ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <Sparkles className="mr-1.5 h-3.5 w-3.5" />}
                  {suggestMutation.isPending ? "Creating suggestions" : "Suggest with AI"}
                </Button>
              )}
            </div>
            {suggestMutation.isError && <p className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive" role="alert">AI suggestions could not be created. You can still choose participants yourself.</p>}

            <div className="divide-y divide-border/60 rounded-xl border border-border/65 bg-background/40">
              {assignmentsQuery.data?.items.map((item) => {
                const selectedParticipantId = selectedParticipantIds[item.speakerId] ?? "";
                const selectedAttendee = attendees.find((attendee) => contactId(attendee) === selectedParticipantId) ?? null;
                const selectedSuggestion = suggestionForAttendee(item, selectedParticipantId);
                const suggested = bestSuggestion(item);
                const pendingThisSpeaker = confirmMutation.isPending && confirmMutation.variables?.speakerId === item.speakerId;
                return (
                  <div key={item.speakerId} className="grid gap-3 p-3.5 lg:grid-cols-[minmax(150px,0.72fr)_minmax(220px,1fr)_auto] lg:items-center">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <p className="truncate text-sm font-semibold">{item.currentDisplayName || item.speakerLabel}</p>
                        {item.confirmedAttendee && <Badge variant="outline" className="border-emerald-500/40 text-[10px] text-emerald-700 dark:text-emerald-300"><Check className="mr-1 h-3 w-3" />Confirmed</Badge>}
                      </div>
                      <p className="mt-1 text-[11px] text-muted-foreground">{item.sourceHint === "microphone" ? "Your microphone" : "Meeting audio"}</p>
                      {!item.confirmedAttendee && item.profileMatch && (
                        <p className="mt-1 text-[11px] leading-4 text-muted-foreground">
                          Local voice profile · {item.profileMatch.displayName}{confidenceLabel(item.profileMatch.confidence) ? ` · ${confidenceLabel(item.profileMatch.confidence)}` : ""}
                        </p>
                      )}
                      {!item.confirmedAttendee && suggested && (
                        <div className="mt-2">
                          <p className="text-[10px] font-semibold uppercase tracking-[0.1em] text-primary">{suggestionLabel(suggested.source)}</p>
                          <p className="mt-0.5 text-[11px] leading-4 text-muted-foreground">{suggested.attendee.name || suggested.attendee.address}{confidenceLabel(suggested.confidence) ? ` · ${confidenceLabel(suggested.confidence)}` : ""}{suggested.reason ? ` · ${suggested.reason}` : ""}</p>
                        </div>
                      )}
                    </div>

                    <div className="min-w-0">
                      <Select value={selectedParticipantId} onValueChange={(participantId) => setSelectedParticipantIds((current) => ({ ...current, [item.speakerId]: participantId }))}>
                        <SelectTrigger className="h-10 min-w-0 bg-background" aria-label={`Participant for ${item.currentDisplayName || item.speakerLabel}`}>
                          <SelectValue placeholder="Choose participant…" />
                        </SelectTrigger>
                        <SelectContent>
                          {attendees.map((attendee) => {
                            const attendeeId = contactId(attendee);
                            const suggestion = suggestionForAttendee(item, attendeeId);
                            return (
                              <SelectItem key={attendeeId} value={attendeeId}>
                                {attendee.name || attendee.address}{attendeeQualifier(attendee)}{suggestion ? ` · ${suggestionLabel(suggestion.source)}` : ""}
                              </SelectItem>
                            );
                          })}
                        </SelectContent>
                      </Select>
                      {selectedAttendee && <p className="mt-1.5 flex min-w-0 items-center gap-1.5 truncate text-[11px] text-muted-foreground"><Mail className="h-3 w-3 shrink-0" />{selectedAttendee.address}</p>}
                    </div>

                    <div className="flex flex-wrap gap-2 lg:justify-end">
                      {item.confirmedAttendee && (
                        <Button type="button" size="sm" variant="ghost" disabled={pendingThisSpeaker} onClick={() => confirmMutation.mutate({ speakerId: item.speakerId, participantId: null, suggestionSource: "manual" })}>Remove</Button>
                      )}
                      <Button
                        type="button"
                        size="sm"
                        disabled={!selectedParticipantId || pendingThisSpeaker}
                        onClick={() => confirmMutation.mutate({
                          speakerId: item.speakerId,
                          participantId: selectedParticipantId,
                          suggestionSource: selectedSuggestion?.source ?? "manual",
                        })}
                      >
                        {pendingThisSpeaker ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <UserCheck className="mr-1.5 h-3.5 w-3.5" />}
                        {item.confirmedAttendee ? "Update" : "Confirm"}
                      </Button>
                    </div>
                  </div>
                );
              })}
            </div>
            {confirmMutation.isError && <p className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive" role="alert">The speaker assignment was not saved. Try again.</p>}
            <p className="flex items-start gap-2 text-[11px] leading-4 text-muted-foreground">
              <Mail className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              Confirmed mappings improve speaker names in the transcript. The email draft separately uses suitable participants from the linked Outlook event, and always shows the recipients for review.
            </p>
          </div>
        )}
      </div>
    </details>
  );
}

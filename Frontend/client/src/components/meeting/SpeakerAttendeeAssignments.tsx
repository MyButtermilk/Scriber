import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  ChevronDown,
  CirclePlay,
  GitMerge,
  Loader2,
  Mail,
  ShieldCheck,
  Sparkles,
  UserCheck,
  Users,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useI18n, type TranslationValues } from "@/i18n";
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
import { apiUrl } from "@/lib/backend";
import { fetchWithTimeout } from "@/lib/fetch-with-timeout";
import { apiRequest } from "@/lib/queryClient";
import {
  initialMeetingParticipantId,
  meetingContactId,
  meetingContactIdentityKeys,
  meetingContactsMatch,
} from "@/lib/meeting-speaker-selection";
import type {
  MeetingSpeakerAssignment,
  MeetingSpeakerAssignmentConfirmationResponse,
  MeetingSpeakerAssignmentsResponse,
  MeetingSpeakerSuggestion,
  OutlookCalendarContact,
  OutlookCalendarEvent,
} from "@/lib/api-types";
import {
  applySpeakerAssignmentConfirmation,
  canonicalMeetingSpeakerMergeSelection,
  meetingSpeakerMergeOptions,
} from "@/lib/meeting-speaker-assignments";
import { refreshAllMeetingSpeakerIdentityCaches } from "@/lib/meeting-cache";

const SUGGESTION_PRIORITY: Record<MeetingSpeakerSuggestion["source"], number> = {
  voice_profile: 0,
  account: 1,
  llm: 2,
};

const CUSTOM_NAME_VALUE = "__meeting_custom_name__";
type Translate = (source: string, values?: TranslationValues) => string;

type ConfirmAssignmentVariables =
  | {
      kind: "participant";
      speakerId: string;
      participantId: string | null;
      suggestionSource: MeetingSpeakerSuggestion["source"] | "manual";
    }
  | {
      kind: "custom";
      speakerId: string;
      displayName: string;
    };

function normalizeCustomName(value: string): string {
  return value.trim().replace(/\s+/g, " ");
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
    const keys = meetingContactIdentityKeys(contact);
    if (keys.length === 0 || keys.some((key) => seen.has(key))) return false;
    keys.forEach((key) => seen.add(key));
    return true;
  });
}

function bestSuggestion(item: MeetingSpeakerAssignment): MeetingSpeakerSuggestion | null {
  return [...item.suggestions].sort((left, right) => {
    const sourceDifference = SUGGESTION_PRIORITY[left.source] - SUGGESTION_PRIORITY[right.source];
    if (sourceDifference !== 0) return sourceDifference;
    return (right.confidence ?? -1) - (left.confidence ?? -1);
  })[0] ?? null;
}

function preselectableSuggestion(item: MeetingSpeakerAssignment): MeetingSpeakerSuggestion | null {
  const suggestion = bestSuggestion(item);
  // Missing metadata means an older backend whose unique match behavior was
  // already safe. A newer backend can explicitly keep ambiguous local matches
  // visible without selecting them for the user.
  if (suggestion?.source === "voice_profile" && item.profileMatch?.canPreselect === false) {
    return null;
  }
  return suggestion;
}

function suggestionLabel(source: MeetingSpeakerSuggestion["source"], t: Translate): string {
  if (source === "voice_profile") return t("Voice match · unconfirmed");
  if (source === "account") return t("Your Outlook account · unconfirmed");
  return t("AI suggestion · unconfirmed");
}

function attendeeQualifier(attendee: OutlookCalendarContact, t: Translate): string {
  const labels: string[] = [];
  if (attendee.isCurrentUser) labels.push(t("you"));
  if (attendee.type === "optional") labels.push(t("optional"));
  if (attendee.response === "declined") labels.push(t("declined invitation"));
  return labels.length > 0 ? ` (${labels.join(", ")})` : "";
}

function confidenceLabel(confidence: number | null, formatNumber: (value: number, options?: Intl.NumberFormatOptions) => string, t: Translate): string {
  if (confidence == null || !Number.isFinite(confidence)) return "";
  return t("{{percent}}% match", { percent: formatNumber(Math.round(Math.max(0, Math.min(1, confidence)) * 100)) });
}

function suggestionForAttendee(
  item: MeetingSpeakerAssignment,
  participantId: string,
  attendees: readonly OutlookCalendarContact[],
): MeetingSpeakerSuggestion | null {
  const attendee = attendees.find((candidate) => meetingContactId(candidate) === participantId);
  if (!attendee) return null;
  return item.suggestions
    .filter((suggestion) => meetingContactsMatch(attendee, suggestion.attendee))
    .sort((left, right) => SUGGESTION_PRIORITY[left.source] - SUGGESTION_PRIORITY[right.source])[0] ?? null;
}

export function SpeakerAttendeeAssignments({
  meetingId,
  calendarEvent,
  playableSpeakerIds,
  onPlaySpeaker,
  onAssignmentsChanged,
}: {
  meetingId: string;
  calendarEvent: OutlookCalendarEvent | null;
  playableSpeakerIds: ReadonlySet<string>;
  onPlaySpeaker: (speakerId: string) => void;
  onAssignmentsChanged: () => void;
}) {
  const { t, formatNumber } = useI18n();
  const queryClient = useQueryClient();
  const [selectedParticipantIds, setSelectedParticipantIds] = useState<Record<string, string>>({});
  const [customNames, setCustomNames] = useState<Record<string, string>>({});
  const [mergeTargetProfileId, setMergeTargetProfileId] = useState("");
  const [mergeSourceProfileId, setMergeSourceProfileId] = useState("");
  const [mergeConfirmationOpen, setMergeConfirmationOpen] = useState(false);
  const [panelOpen, setPanelOpen] = useState(true);
  const assignmentsQuery = useQuery<MeetingSpeakerAssignmentsResponse>({
    queryKey: ["/api/meetings", meetingId, "speaker-assignments"],
    queryFn: ({ signal }) => fetchAssignments(meetingId, signal),
    enabled: Boolean(meetingId),
    staleTime: 10_000,
  });
  const event = assignmentsQuery.data?.calendarEvent ?? calendarEvent;
  const attendees = useMemo(() => event ? attendeeOptions(event) : [], [event]);
  const mergeOptions = useMemo(
    () => meetingSpeakerMergeOptions(assignmentsQuery.data?.items ?? []),
    [assignmentsQuery.data?.items],
  );
  const unresolvedCount = assignmentsQuery.data?.items.filter(
    (item) => !item.confirmedAttendee && !item.confirmedCustomName,
  ).length ?? 0;

  useEffect(() => {
    const items = assignmentsQuery.data?.items ?? [];
    setSelectedParticipantIds((current) => {
      const next: Record<string, string> = {};
      for (const item of items) {
        const currentId = current[item.speakerId];
        const suggestedAttendee = preselectableSuggestion(item)?.attendee ?? null;
        // Suggestions are serialized independently from the frozen Outlook
        // attendee list. Map them through aliases/email instead of assuming
        // both payloads use the same participant id, otherwise a valid local
        // Voice Library match can render as an empty Select value.
        if (item.confirmedCustomName) {
          next[item.speakerId] = CUSTOM_NAME_VALUE;
        } else if (item.confirmedAttendee) {
          next[item.speakerId] = initialMeetingParticipantId(
            attendees,
            "",
            item.confirmedAttendee,
            suggestedAttendee,
          );
        } else if (currentId === CUSTOM_NAME_VALUE) {
          next[item.speakerId] = CUSTOM_NAME_VALUE;
        } else {
          next[item.speakerId] = initialMeetingParticipantId(
            attendees,
            currentId,
            null,
            suggestedAttendee,
          ) || (attendees.length === 0 ? CUSTOM_NAME_VALUE : "");
        }
      }
      return next;
    });
    setCustomNames((current) => {
      const next: Record<string, string> = {};
      for (const item of items) {
        next[item.speakerId] = item.confirmedCustomName ?? current[item.speakerId] ?? "";
      }
      return next;
    });
  }, [assignmentsQuery.data?.items, attendees]);

  useEffect(() => {
    const availableIds = new Set(mergeOptions.map((option) => option.profileId));
    if (mergeTargetProfileId && !availableIds.has(mergeTargetProfileId)) {
      setMergeTargetProfileId("");
    }
    if (
      mergeSourceProfileId
      && (!availableIds.has(mergeSourceProfileId) || mergeSourceProfileId === mergeTargetProfileId)
    ) {
      setMergeSourceProfileId("");
    }
  }, [mergeOptions, mergeSourceProfileId, mergeTargetProfileId]);

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
    mutationFn: async (variables: ConfirmAssignmentVariables) => {
      const body = variables.kind === "custom"
        ? { displayName: variables.displayName, confirmed: true }
        : {
            participantId: variables.participantId,
            confirmed: true,
            suggestionSource: variables.suggestionSource,
          };
      const response = await apiRequest("PATCH", `/api/meetings/${meetingId}/speakers/${variables.speakerId}/attendee`, body);
      return response.json() as Promise<MeetingSpeakerAssignmentConfirmationResponse>;
    },
    onSuccess: (response, variables) => {
      queryClient.setQueryData<MeetingSpeakerAssignmentsResponse>(
        ["/api/meetings", meetingId, "speaker-assignments"],
        (current) => applySpeakerAssignmentConfirmation(current, response),
      );
      if (!response.assignment.source) {
        setCustomNames((current) => ({ ...current, [variables.speakerId]: "" }));
        setSelectedParticipantIds((current) => ({
          ...current,
          [variables.speakerId]: attendees.length === 0 ? CUSTOM_NAME_VALUE : "",
        }));
      }
      void queryClient.invalidateQueries({ queryKey: ["/api/meetings", meetingId, "email-preview"], exact: true });
      onAssignmentsChanged();
    },
  });

  const requestedMergeTarget = mergeOptions.find((option) => option.profileId === mergeTargetProfileId) ?? null;
  const requestedMergeSource = mergeOptions.find((option) => option.profileId === mergeSourceProfileId) ?? null;
  const {
    target: mergeTarget,
    source: mergeSource,
    directionChanged: mergeDirectionChanged,
  } = canonicalMeetingSpeakerMergeSelection(requestedMergeTarget, requestedMergeSource);

  const mergeMutation = useMutation({
    mutationFn: async () => {
      if (!mergeTarget || !mergeSource) throw new Error(t("Choose two speaker identities to merge."));
      const response = await apiRequest("POST", "/api/meetings/speaker-profiles/merge", {
        targetProfileId: mergeTarget.profileId,
        sourceProfileId: mergeSource.profileId,
      });
      return response.json() as Promise<{ targetProfileId: string; mergedProfileId: string }>;
    },
    onSuccess: async (payload) => {
      setMergeConfirmationOpen(false);
      setMergeTargetProfileId(payload.targetProfileId);
      setMergeSourceProfileId("");
      await refreshAllMeetingSpeakerIdentityCaches(queryClient);
    },
  });

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
            <p className="text-sm font-semibold">{t("Name detected speakers")}</p>
            <p className="mt-0.5 truncate text-xs text-muted-foreground">
              {assignmentsQuery.isLoading
                ? t("Checking saved voices and participant details…")
                : unresolvedCount > 0
                  ? t(unresolvedCount === 1 ? "{{count}} speaker needs your confirmation" : "{{count}} speakers need your confirmation", { count: formatNumber(unresolvedCount) })
                  : t("All detected speaker names have been reviewed")}
            </p>
          </div>
        </div>
        <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground transition-transform duration-[var(--duration-quick)] group-open:rotate-180 motion-reduce:transition-none" />
      </summary>

      <div className="border-t border-border/60 p-4">
        {assignmentsQuery.isLoading ? (
          <div className="grid gap-2" role="status" aria-label={t("Loading speaker assignments")}>
            {[0, 1].map((item) => <div key={item} className="h-24 animate-pulse rounded-xl bg-muted motion-reduce:animate-none" />)}
          </div>
        ) : assignmentsQuery.isError ? (
          <div className="rounded-xl border border-destructive/30 bg-destructive/10 p-3" role="alert">
            <p className="text-sm font-medium text-destructive">{t("Speaker suggestions could not be loaded.")}</p>
            <p className="mt-1 text-xs text-muted-foreground">{t("The transcript and any linked Outlook event remain saved.")}</p>
            <Button type="button" size="sm" variant="outline" className="mt-3" onClick={() => void assignmentsQuery.refetch()}>{t("Try again")}</Button>
          </div>
        ) : (assignmentsQuery.data?.items.length ?? 0) === 0 ? (
          <p className="rounded-xl border border-dashed border-border/70 px-4 py-6 text-center text-sm text-muted-foreground">{t("No separate speakers were detected in this transcript.")}</p>
        ) : (
          <div className="space-y-3">
            <div className="flex flex-col gap-3 rounded-xl border border-border/60 bg-background/45 p-3 sm:flex-row sm:items-start sm:justify-between">
              <div className="flex min-w-0 items-start gap-2.5">
                <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
                <div>
                  <p className="text-xs font-semibold">
                    {event ? t("Saved voice and account matches run first on this device.") : t("Give each detected speaker a clear meeting name.")}
                  </p>
                  <p className="mt-1 text-[11px] leading-4 text-muted-foreground">
                    {event
                      ? t("AI suggestions are optional. If you request them, Scriber sends short transcript excerpts and participant names to {{model}}. Outlook email addresses are not sent. Every suggestion stays unconfirmed until you approve it. You can also enter a person, team, or room name that stays only in this meeting.", { model: assignmentsQuery.data?.llmModel || t("your configured summary model") })
                      : t("Enter a person, team, room, or shared-microphone name. It stays only in this meeting and does not change the Voice Library or create an email recipient.")}
                  </p>
                </div>
              </div>
              {unresolvedCount > 0 && assignmentsQuery.data?.llmSuggestionAvailable && (
                <Button type="button" size="sm" variant="outline" className="shrink-0" disabled={suggestMutation.isPending} onClick={() => suggestMutation.mutate()}>
                  {suggestMutation.isPending ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <Sparkles className="mr-1.5 h-3.5 w-3.5" />}
                  {suggestMutation.isPending ? t("Creating suggestions") : t("Suggest with AI")}
                </Button>
              )}
            </div>
            {suggestMutation.isError && <p className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive" role="alert">{t("AI suggestions could not be created. You can still choose participants yourself.")}</p>}

            <div className="divide-y divide-border/60 rounded-xl border border-border/65 bg-background/40">
              {assignmentsQuery.data?.items.map((item) => {
                const selectedParticipantId = selectedParticipantIds[item.speakerId] ?? "";
                const isCustomName = selectedParticipantId === CUSTOM_NAME_VALUE;
                const customName = customNames[item.speakerId] ?? "";
                const normalizedCustomName = normalizeCustomName(customName);
                const selectedAttendee = attendees.find((attendee) => meetingContactId(attendee) === selectedParticipantId) ?? null;
                const selectedSuggestion = suggestionForAttendee(item, selectedParticipantId, attendees);
                const suggested = bestSuggestion(item);
                const itemResolved = Boolean(item.confirmedAttendee || item.confirmedCustomName);
                const pendingThisSpeaker = confirmMutation.isPending && confirmMutation.variables?.speakerId === item.speakerId;
                const customNameInputId = `meeting-speaker-name-${item.speakerId}`;
                return (
                  <div key={item.speakerId} className="grid gap-3 p-3.5 lg:grid-cols-[minmax(150px,0.72fr)_minmax(220px,1fr)_auto] lg:items-start">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <p className="truncate text-sm font-semibold">{item.currentDisplayName || item.speakerLabel}</p>
                        {itemResolved && <Badge variant="outline" className="border-emerald-500/40 text-[10px] text-emerald-700 dark:text-emerald-300"><Check className="mr-1 h-3 w-3" />{item.confirmedCustomName ? t("Meeting name") : t("Confirmed")}</Badge>}
                      </div>
                      <p className="mt-1 text-[11px] text-muted-foreground">{item.sourceHint === "microphone" ? t("Your microphone") : t("Meeting audio")}</p>
                      <Button
                        type="button"
                        size="sm"
                        variant="ghost"
                        className="mt-1 h-7 px-2 text-[11px]"
                        disabled={!playableSpeakerIds.has(item.speakerId)}
                        onClick={() => onPlaySpeaker(item.speakerId)}
                        title={playableSpeakerIds.has(item.speakerId) ? t("Play up to 8 seconds from this speaker") : t("No saved audio sample is available")}
                      >
                        <CirclePlay className="mr-1.5 h-3.5 w-3.5" />{t("Play sample")}
                      </Button>
                      {!itemResolved && item.profileMatch && (
                        <p className="mt-1 text-[11px] leading-4 text-muted-foreground">
                          {t("Local voice profile")} · {item.profileMatch.displayName}{confidenceLabel(item.profileMatch.confidence, formatNumber, t) ? ` · ${confidenceLabel(item.profileMatch.confidence, formatNumber, t)}` : ""}{item.profileMatch.evidenceCount ? ` · ${t(item.profileMatch.evidenceCount === 1 ? "{{count}} saved sample" : "{{count}} saved samples", { count: formatNumber(item.profileMatch.evidenceCount) })}` : ""}{item.profileMatch.canPreselect === false ? ` · ${t("review manually")}` : ""}
                        </p>
                      )}
                      {!itemResolved && suggested && (
                        <div className="mt-2">
                          <p className="text-[10px] font-semibold uppercase tracking-[0.1em] text-primary">{suggestionLabel(suggested.source, t)}</p>
                          <p className="mt-0.5 text-[11px] leading-4 text-muted-foreground">{suggested.attendee.name || suggested.attendee.address}{confidenceLabel(suggested.confidence, formatNumber, t) ? ` · ${confidenceLabel(suggested.confidence, formatNumber, t)}` : ""}{suggested.reason ? ` · ${suggested.reason}` : ""}</p>
                        </div>
                      )}
                    </div>

                    <div className="min-w-0">
                      <Select value={selectedParticipantId} onValueChange={(participantId) => setSelectedParticipantIds((current) => ({ ...current, [item.speakerId]: participantId }))}>
                        <SelectTrigger className="h-10 min-w-0 bg-background" aria-label={t("Name source for {{speaker}}", { speaker: item.currentDisplayName || item.speakerLabel })}>
                          <SelectValue placeholder={attendees.length > 0 ? t("Choose participant or enter a name…") : t("Enter a meeting name…")} />
                        </SelectTrigger>
                        <SelectContent>
                          {attendees.map((attendee) => {
                            const attendeeId = meetingContactId(attendee);
                            const suggestion = suggestionForAttendee(item, attendeeId, attendees);
                            return (
                              <SelectItem key={attendeeId} value={attendeeId}>
                                {attendee.name || attendee.address}{attendeeQualifier(attendee, t)}{suggestion ? ` · ${suggestionLabel(suggestion.source, t)}` : ""}
                              </SelectItem>
                            );
                          })}
                          <SelectItem value={CUSTOM_NAME_VALUE}>{t("Enter another name…")}</SelectItem>
                        </SelectContent>
                      </Select>
                      {selectedAttendee && <p className="mt-1.5 flex min-w-0 items-center gap-1.5 truncate text-[11px] text-muted-foreground"><Mail className="h-3 w-3 shrink-0" />{selectedAttendee.address}</p>}
                      {isCustomName && (
                        <div className="mt-2">
                          <label htmlFor={customNameInputId} className="text-[11px] font-medium text-foreground">{t("Speaker name")}</label>
                          <Input
                            id={customNameInputId}
                            value={customName}
                            maxLength={120}
                            placeholder={t("For example: Project team or Berlin room")}
                            className="mt-1 h-9 bg-background text-sm"
                            onChange={(event) => setCustomNames((current) => ({
                              ...current,
                              [item.speakerId]: event.target.value,
                            }))}
                          />
                          <p className="mt-1.5 text-[11px] leading-4 text-muted-foreground">
                            {t("Only this meeting changes. Voice profiles, Outlook contacts, and email recipients stay untouched.")}
                          </p>
                        </div>
                      )}
                      {!itemResolved && selectedSuggestion?.source === "voice_profile" && !isCustomName && (
                        <p className="mt-1.5 flex items-start gap-1.5 text-[11px] leading-4 text-primary">
                          <ShieldCheck className="mt-0.5 h-3 w-3 shrink-0" />
                          {t("Preselected from a saved voice match. Confirm to apply it.")}
                        </p>
                      )}
                    </div>

                    <div className="flex flex-wrap gap-2 lg:justify-end">
                      {itemResolved && (
                        <Button type="button" size="sm" variant="ghost" disabled={pendingThisSpeaker} onClick={() => confirmMutation.mutate({ kind: "participant", speakerId: item.speakerId, participantId: null, suggestionSource: "manual" })}>{t("Remove")}</Button>
                      )}
                      <Button
                        type="button"
                        size="sm"
                        disabled={pendingThisSpeaker || (isCustomName ? !normalizedCustomName : !selectedParticipantId)}
                        onClick={() => {
                          if (isCustomName) {
                            confirmMutation.mutate({
                              kind: "custom",
                              speakerId: item.speakerId,
                              displayName: normalizedCustomName,
                            });
                            return;
                          }
                          confirmMutation.mutate({
                            kind: "participant",
                            speakerId: item.speakerId,
                            participantId: selectedParticipantId,
                            suggestionSource: selectedSuggestion?.source ?? "manual",
                          });
                        }}
                      >
                        {pendingThisSpeaker ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <UserCheck className="mr-1.5 h-3.5 w-3.5" />}
                        {isCustomName ? t("Save name") : itemResolved ? t("Update") : t("Confirm")}
                      </Button>
                    </div>
                  </div>
                );
              })}
            </div>
            {mergeOptions.length >= 2 && (
              <div className="rounded-xl border border-border/65 bg-background/40 p-3.5">
                <div className="flex items-start gap-2.5">
                  <GitMerge className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
                  <div>
                    <p className="text-xs font-semibold">{t("Merge duplicate speaker identities")}</p>
                    <p className="mt-1 text-[11px] leading-4 text-muted-foreground">
                      {t("If two detected speakers are the same voice, keep the correct identity and merge the duplicate into it. Saved samples and future voice matches are combined permanently. If only one profile has a saved name, Scriber always preserves it.")}
                    </p>
                  </div>
                </div>
                <div className="mt-3 grid gap-2 sm:grid-cols-2">
                  <Select value={mergeTargetProfileId} onValueChange={(profileId) => {
                    setMergeTargetProfileId(profileId);
                    if (profileId === mergeSourceProfileId) setMergeSourceProfileId("");
                  }}>
                    <SelectTrigger className="h-9 min-w-0 bg-background text-xs" aria-label={t("Speaker identity to keep")}>
                      <SelectValue placeholder={t("Keep speaker…")} />
                    </SelectTrigger>
                    <SelectContent>
                      {mergeOptions.map((option) => (
                        <SelectItem key={option.profileId} value={option.profileId}>
                          {option.displayName}{option.speakerLabel && option.speakerLabel !== option.displayName ? ` · ${option.speakerLabel}` : ""}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <Select value={mergeSourceProfileId} onValueChange={setMergeSourceProfileId}>
                    <SelectTrigger className="h-9 min-w-0 bg-background text-xs" aria-label={t("Duplicate speaker identity to merge")}>
                      <SelectValue placeholder={t("Merge duplicate…")} />
                    </SelectTrigger>
                    <SelectContent>
                      {mergeOptions.filter((option) => option.profileId !== mergeTargetProfileId).map((option) => (
                        <SelectItem key={option.profileId} value={option.profileId}>
                          {option.displayName}{option.speakerLabel && option.speakerLabel !== option.displayName ? ` · ${option.speakerLabel}` : ""}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    className="h-9 sm:col-span-2"
                    disabled={!mergeTarget || !mergeSource || mergeMutation.isPending}
                    onClick={() => {
                      mergeMutation.reset();
                      setMergeConfirmationOpen(true);
                    }}
                  >
                    <GitMerge className="mr-1.5 h-3.5 w-3.5" />{t("Merge speakers")}
                  </Button>
                </div>
                {mergeMutation.isError && (
                  <p className="mt-2 rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive" role="alert">
                    {t("The speaker identities could not be merged. Nothing was changed.")}
                  </p>
                )}
              </div>
            )}
            {confirmMutation.isError && <p className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive" role="alert">{t("The speaker assignment was not saved. Try again.")}</p>}
            <p className="flex items-start gap-2 text-[11px] leading-4 text-muted-foreground">
              <Mail className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              {t("Confirmed mappings improve speaker names in the transcript. Email drafts use only suitable participants from the linked Outlook event—never a freely entered speaker name—and always show recipients for review.")}
            </p>
          </div>
        )}
      </div>
      <AlertDialog open={mergeConfirmationOpen} onOpenChange={setMergeConfirmationOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("Merge these speaker identities permanently?")}</AlertDialogTitle>
            <AlertDialogDescription>
              {mergeTarget && mergeSource
                ? <>
                    {mergeDirectionChanged && <>{t("To preserve the only saved Voice Library name, Scriber adjusted the merge direction.")} </>}
                    {t("Scriber will keep {{target}} and merge {{source}} into it. Their saved voice samples and future matches become one identity. This cannot be undone automatically.", { target: mergeTarget.displayName, source: mergeSource.displayName })}
                  </>
                : t("Choose the speaker identity to keep and the duplicate to merge.")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          {mergeMutation.isError && (
            <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive" role="alert">
              <p className="font-medium">{t("The speaker identities could not be merged.")}</p>
              <p className="mt-1 text-xs">{mergeMutation.error.message || t("Nothing was changed. Try again.")}</p>
            </div>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={mergeMutation.isPending}>{t("Cancel")}</AlertDialogCancel>
            <AlertDialogAction
              disabled={!mergeTarget || !mergeSource || mergeMutation.isPending}
              onClick={(event) => {
                event.preventDefault();
                mergeMutation.mutate();
              }}
            >
              {mergeMutation.isPending && <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />}
              {t("Keep {{speaker}} and merge", { speaker: mergeTarget?.displayName || t("speaker") })}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </details>
  );
}

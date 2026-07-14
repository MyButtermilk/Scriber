import {
  CalendarClock,
  Check,
  Clock3,
  ExternalLink,
  Mail,
  MapPin,
  RefreshCw,
  Users,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import type {
  OutlookCalendarContact,
  OutlookCalendarEvent,
  OutlookCalendarEventsResponse,
  OutlookCalendarStatus,
} from "@/lib/api-types";
import { cn } from "@/lib/utils";

function formatEventTime(startAt: string, endAt: string): string {
  const start = new Date(startAt);
  const end = new Date(endAt);
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return "Time unavailable";
  const formatter = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
  return `${formatter.format(start)}–${formatter.format(end)}`;
}

async function openMeetingUrl(rawUrl: string): Promise<void> {
  let url: URL;
  try {
    url = new URL(rawUrl);
  } catch {
    return;
  }
  if (url.protocol !== "https:") return;
  try {
    const { openUrl } = await import("@tauri-apps/plugin-opener");
    await openUrl(url.toString());
  } catch {
    window.open(url.toString(), "_blank", "noopener,noreferrer");
  }
}

function contactIdentityKeys(contact: OutlookCalendarContact): string[] {
  return [contact.address, ...(contact.aliases ?? []), contact.participantId ?? ""]
    .map((value) => value.trim().toLocaleLowerCase())
    .filter(Boolean);
}

function eventContactCount(event: OutlookCalendarEvent): number {
  const contacts = [event.organizer, ...event.participants];
  const seen = new Set<string>();
  let people = 0;
  for (const contact of contacts) {
    if (!contact || contact.type === "resource") continue;
    const keys = contactIdentityKeys(contact);
    if (keys.length === 0 || keys.some((key) => seen.has(key))) continue;
    keys.forEach((key) => seen.add(key));
    people += 1;
  }
  return people;
}

function participantLabel(participant: OutlookCalendarEvent["participants"][number]): string | undefined {
  const labels: string[] = [];
  if (participant.isCurrentUser) labels.push("You");
  if (participant.type === "optional") labels.push("Optional");
  if (participant.type === "resource") labels.push("Room or resource");
  if (participant.response === "declined") labels.push("Declined");
  else if (participant.response === "tentativelyAccepted") labels.push("Tentative");
  return labels.length > 0 ? labels.join(" · ") : undefined;
}

function visibleParticipants(event: OutlookCalendarEvent): OutlookCalendarEvent["participants"] {
  const organizerKeys = new Set<string>();
  if (event.organizer) {
    contactIdentityKeys(event.organizer).forEach((key) => organizerKeys.add(key));
  }
  const seen = new Set<string>();
  return event.participants.filter((participant) => {
    const keys = contactIdentityKeys(participant);
    if (keys.length === 0 || keys.some((key) => organizerKeys.has(key) || seen.has(key))) return false;
    keys.forEach((key) => seen.add(key));
    return true;
  });
}

function ContactLine({ name, address, label }: { name: string; address: string; label?: string }) {
  return (
    <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-start gap-3 py-1.5 text-xs">
      <div className="min-w-0">
        <p className="truncate font-medium text-foreground">{name || address}</p>
        {name && <p className="mt-0.5 truncate text-muted-foreground">{address}</p>}
      </div>
      {label && <span className="rounded-full border border-border/70 px-2 py-0.5 text-[10px] font-medium text-muted-foreground">{label}</span>}
    </div>
  );
}

export function OutlookMeetingPicker({
  status,
  events,
  statusLoading,
  statusError,
  eventsLoading,
  eventsError,
  refreshing,
  selectedEventId,
  onSelect,
  onRefresh,
  onOpenSettings,
}: {
  status?: OutlookCalendarStatus;
  events?: OutlookCalendarEventsResponse;
  statusLoading: boolean;
  statusError: boolean;
  eventsLoading: boolean;
  eventsError: boolean;
  refreshing: boolean;
  selectedEventId: string;
  onSelect: (event: OutlookCalendarEvent | null) => void;
  onRefresh: () => void;
  onOpenSettings: () => void;
}) {
  const selectedEvent = events?.items.find((event) => event.id === selectedEventId) ?? null;

  return (
    <section
      className="overflow-hidden rounded-2xl border border-border/70 bg-background/55"
      aria-labelledby="outlook-meeting-picker-title"
    >
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border/60 px-4 py-3.5">
        <div className="flex min-w-0 items-start gap-3">
          <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-primary/10 text-primary">
            <CalendarClock className="h-4 w-4" aria-hidden="true" />
          </span>
          <div className="min-w-0">
            <h3 id="outlook-meeting-picker-title" className="text-sm font-semibold">Today in Outlook</h3>
            <p className="mt-0.5 text-xs leading-5 text-muted-foreground">
              {events?.account?.address
                ? `Pick the calendar event for ${events.account.name ? `${events.account.name} (${events.account.address})` : events.account.address}.`
                : "Pick the calendar event that belongs to this recording."}
            </p>
          </div>
        </div>
        {status?.connected && (
          <Button type="button" size="sm" variant="ghost" disabled={refreshing} onClick={onRefresh}>
            <RefreshCw className={cn("mr-1.5 h-3.5 w-3.5", refreshing && "animate-spin motion-reduce:animate-none")} />
            {refreshing ? "Refreshing" : "Refresh calendar"}
          </Button>
        )}
      </div>

      {statusLoading ? (
        <div className="grid gap-2 p-4" role="status" aria-label="Checking Outlook calendar">
          <div className="h-4 w-40 animate-pulse rounded bg-muted motion-reduce:animate-none" />
          <div className="h-16 animate-pulse rounded-xl bg-muted/70 motion-reduce:animate-none" />
        </div>
      ) : statusError ? (
        <div className="p-4">
          <p className="text-sm font-medium text-destructive">Outlook could not be checked.</p>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">Restart Scriber or try the connection again in Settings.</p>
          <Button type="button" size="sm" variant="outline" className="mt-3" onClick={onRefresh}>Try again</Button>
        </div>
      ) : !status?.configured ? (
        <div className="p-4">
          <p className="text-sm font-medium">Outlook is not available in this release.</p>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">Open Meeting settings for simple setup help.</p>
          <Button type="button" size="sm" variant="outline" className="mt-3" onClick={onOpenSettings}>Open settings</Button>
        </div>
      ) : status.authorizationPending ? (
        <div className="p-4">
          <p className="text-sm font-medium">Finish Outlook sign-in in your browser.</p>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">Today&apos;s events stay hidden until Microsoft confirms which account is connected.</p>
          <Button type="button" size="sm" variant="outline" className="mt-3" onClick={onOpenSettings}>View connection status</Button>
        </div>
      ) : !status.connected ? (
        <div className="p-4">
          <p className="text-sm font-medium">Connect Outlook to use meeting details.</p>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">Scriber requests read-only calendar access and never receives your Microsoft password.</p>
          <Button type="button" size="sm" variant="outline" className="mt-3" onClick={onOpenSettings}>Connect in Settings</Button>
        </div>
      ) : eventsLoading ? (
        <div className="grid gap-2 p-4" role="status" aria-label="Loading today's Outlook meetings">
          {[0, 1].map((item) => <div key={item} className="h-[70px] animate-pulse rounded-xl bg-muted/70 motion-reduce:animate-none" />)}
        </div>
      ) : eventsError ? (
        <div className="p-4" role="alert">
          <p className="text-sm font-medium text-destructive">Today&apos;s meetings could not be loaded.</p>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">Your existing meeting setup is unchanged. Refresh the calendar to try again.</p>
          <Button type="button" size="sm" variant="outline" className="mt-3" disabled={refreshing} onClick={onRefresh}>Refresh calendar</Button>
        </div>
      ) : (events?.items.length ?? 0) === 0 ? (
        <div className="p-4">
          <p className="text-sm font-medium">No Outlook meetings today.</p>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">You can still start an unlinked meeting and enter its title yourself.</p>
        </div>
      ) : (
        <div className="p-3">
          <div className="max-h-72 space-y-2 overflow-y-auto pr-1" role="list" aria-label="Today's Outlook meetings">
            {events?.items.map((event) => {
              const selected = event.id === selectedEventId;
              return (
                <div
                  key={event.id}
                  role="listitem"
                  className={cn(
                    "rounded-xl border transition-colors duration-[var(--duration-quick)] motion-reduce:transition-none",
                    selected ? "border-primary/55 bg-primary/5" : "border-border/65 bg-muted/20 hover:bg-muted/40",
                  )}
                >
                  <button
                    type="button"
                    className="flex w-full items-start gap-3 px-3 py-3 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2"
                    aria-pressed={selected}
                    onClick={() => onSelect(event)}
                  >
                    <span className={cn(
                      "mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full border",
                      selected ? "border-primary bg-primary text-primary-foreground" : "border-border bg-background",
                    )}>
                      {selected && <Check className="h-3 w-3" aria-hidden="true" />}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm font-semibold">{event.subject || "Untitled Outlook meeting"}</span>
                      <span className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
                        <span className="inline-flex items-center gap-1"><Clock3 className="h-3.5 w-3.5" />{event.isAllDay ? "All day" : formatEventTime(event.start_at, event.end_at)}</span>
                        <span className="inline-flex items-center gap-1"><Users className="h-3.5 w-3.5" />{eventContactCount(event)} people</span>
                      </span>
                    </span>
                  </button>
                  {selected && (
                    <div className="border-t border-border/60 px-3 pb-3 pt-2.5">
                      <div className="flex items-center justify-between gap-3">
                        <p className="text-[11px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">Participants</p>
                        <button type="button" className="text-[11px] font-medium text-muted-foreground hover:text-foreground" onClick={() => onSelect(null)}>Use no calendar event</button>
                      </div>
                      <div className="mt-1 max-h-48 divide-y divide-border/50 overflow-y-auto pr-1">
                        {event.organizer && <ContactLine name={event.organizer.name} address={event.organizer.address} label={event.organizer.isCurrentUser ? "You · organizer" : "Organizer"} />}
                        {visibleParticipants(event).map((participant) => (
                          <ContactLine
                            key={participant.participantId || participant.address}
                            name={participant.name}
                            address={participant.address}
                            label={participantLabel(participant)}
                          />
                        ))}
                      </div>
                      {(event.location || event.join_url) && (
                        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-border/50 pt-2.5 text-xs text-muted-foreground">
                          {event.location && <span className="inline-flex min-w-0 items-center gap-1.5"><MapPin className="h-3.5 w-3.5 shrink-0" /><span className="truncate">{event.location}</span></span>}
                          {event.join_url && (
                            <button type="button" className="inline-flex items-center gap-1.5 font-medium text-primary hover:underline" onClick={() => void openMeetingUrl(event.join_url)}>
                              <ExternalLink className="h-3.5 w-3.5" />Open online meeting
                            </button>
                          )}
                        </div>
                      )}
                      {event.participants.length === 0 && !event.organizer && (
                        <p className="mt-2 flex items-center gap-1.5 text-xs text-muted-foreground"><Mail className="h-3.5 w-3.5" />No participant addresses are stored for this event.</p>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
          {events?.truncated && <p className="px-1 pt-2 text-[11px] leading-4 text-muted-foreground">Only the first calendar entries for today are shown. Refresh after Outlook changes to get the latest list.</p>}
          {!selectedEvent && <p className="px-1 pt-2 text-[11px] leading-4 text-muted-foreground">Select an event to copy its title and keep its participants with the meeting.</p>}
        </div>
      )}
    </section>
  );
}

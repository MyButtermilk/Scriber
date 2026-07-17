import {
  AlertTriangle,
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
import { useI18n, type TranslationValues } from "@/i18n";
import type {
  OutlookCalendarContact,
  OutlookCalendarEvent,
  OutlookCalendarEventsResponse,
  OutlookCalendarStatus,
} from "@/lib/api-types";
import { cn } from "@/lib/utils";

type Translate = (source: string, values?: TranslationValues) => string;
type FormatDate = (value: Date | number | string, options?: Intl.DateTimeFormatOptions) => string;

function formatEventTime(startAt: string, endAt: string, formatDate: FormatDate, t: Translate): string {
  const start = new Date(startAt);
  const end = new Date(endAt);
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) return t("Time unavailable");
  const options: Intl.DateTimeFormatOptions = { hour: "2-digit", minute: "2-digit" };
  return `${formatDate(start, options)}–${formatDate(end, options)}`;
}

function sameLocalDate(left: Date, right: Date): boolean {
  return left.getFullYear() === right.getFullYear()
    && left.getMonth() === right.getMonth()
    && left.getDate() === right.getDate();
}

function formatSyncMoment(value: string, formatDate: FormatDate, t: Translate, now = new Date()): string | null {
  const syncedAt = new Date(value);
  if (!value || Number.isNaN(syncedAt.getTime())) return null;
  const time = formatDate(syncedAt, { hour: "2-digit", minute: "2-digit" });
  if (sameLocalDate(syncedAt, now)) return t("today at {{time}}", { time });
  const yesterday = new Date(now);
  yesterday.setDate(yesterday.getDate() - 1);
  if (sameLocalDate(syncedAt, yesterday)) return t("yesterday at {{time}}", { time });
  const date = formatDate(syncedAt, {
    day: "numeric",
    month: "short",
    ...(syncedAt.getFullYear() === now.getFullYear() ? {} : { year: "numeric" as const }),
  });
  return t("{{date}} at {{time}}", { date, time });
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

function participantLabel(participant: OutlookCalendarEvent["participants"][number], t: Translate): string | undefined {
  const labels: string[] = [];
  if (participant.isCurrentUser) labels.push(t("You"));
  if (participant.type === "optional") labels.push(t("Optional"));
  if (participant.type === "resource") labels.push(t("Room or resource"));
  if (participant.response === "declined") labels.push(t("Declined"));
  else if (participant.response === "tentativelyAccepted") labels.push(t("Tentative"));
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
  selectionNeedsReview,
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
  selectionNeedsReview: boolean;
  onSelect: (event: OutlookCalendarEvent | null) => void;
  onRefresh: () => void;
  onOpenSettings: () => void;
}) {
  const { t, formatDate, formatNumber } = useI18n();
  const selectedEvent = events?.items.find((event) => event.id === selectedEventId) ?? null;
  const lastSyncMoment = formatSyncMoment(events?.lastSyncAt || status?.lastSyncAt || "", formatDate, t);
  const showSavedCalendarWarning = Boolean(
    status?.connected && events && (status.lastError || eventsError),
  );

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
            <h3 id="outlook-meeting-picker-title" className="text-sm font-semibold">{t("Today in Outlook")}</h3>
            <p className="mt-0.5 text-xs leading-5 text-muted-foreground">
              {events?.account?.address
                ? t("Pick the calendar event for {{account}}.", { account: events.account.name ? `${events.account.name} (${events.account.address})` : events.account.address })
                : t("Pick the calendar event that belongs to this recording.")}
            </p>
            {status?.connected && lastSyncMoment && (
              <p className="mt-1 inline-flex items-center gap-1.5 text-[11px] leading-4 text-muted-foreground">
                <Clock3 className="h-3 w-3" aria-hidden="true" />
                {t("Calendar updated {{moment}}.", { moment: lastSyncMoment })}
              </p>
            )}
          </div>
        </div>
        {status?.connected && (
          <Button type="button" size="sm" variant="ghost" disabled={refreshing} onClick={onRefresh}>
            <RefreshCw className={cn("mr-1.5 h-3.5 w-3.5", refreshing && "animate-spin motion-reduce:animate-none")} />
            {refreshing ? t("Refreshing") : t("Refresh calendar")}
          </Button>
        )}
      </div>

      {showSavedCalendarWarning && (
        <div className="mx-3 mt-3 flex items-start gap-2.5 rounded-xl border border-amber-300/60 bg-amber-500/10 px-3 py-2.5 text-amber-950 dark:text-amber-100" role="alert">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          <div className="min-w-0">
            <p className="text-xs font-semibold">{t("Outlook may be out of date.")}</p>
            <p className="mt-0.5 text-[11px] leading-4">
              {lastSyncMoment
                ? t("Scriber is showing saved meetings from {{moment}}. Changes made in Outlook since then may be missing.", { moment: lastSyncMoment })
                : t("Scriber is showing the last saved calendar. Recent Outlook changes may be missing.")}
            </p>
          </div>
        </div>
      )}

      {selectionNeedsReview && (
        <div className="mx-3 mt-3 flex items-start gap-2.5 rounded-xl border border-amber-300/60 bg-amber-500/10 px-3 py-2.5 text-amber-950 dark:text-amber-100" role="alert">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
          <div className="min-w-0">
            <p className="text-xs font-semibold">{t("Choose the Outlook meeting again.")}</p>
            <p className="mt-0.5 text-[11px] leading-4">
              {t("The event you selected is no longer in today's calendar. It may have moved or been cancelled. Your title was kept, but participants are no longer attached.")}
            </p>
            <button
              type="button"
              className="mt-1.5 text-[11px] font-semibold underline-offset-4 hover:underline focus-visible:rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-700"
              onClick={() => onSelect(null)}
            >
              {t("Continue without Outlook")}
            </button>
          </div>
        </div>
      )}

      {statusLoading ? (
        <div className="grid gap-2 p-4" role="status" aria-label={t("Checking Outlook calendar")}>
          <div className="h-4 w-40 animate-pulse rounded bg-muted motion-reduce:animate-none" />
          <div className="h-16 animate-pulse rounded-xl bg-muted/70 motion-reduce:animate-none" />
        </div>
      ) : statusError ? (
        <div className="p-4">
          <p className="text-sm font-medium text-destructive">{t("Outlook could not be checked.")}</p>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">{t("Restart Scriber or try the connection again in Settings.")}</p>
          <Button type="button" size="sm" variant="outline" className="mt-3" onClick={onRefresh}>{t("Try again")}</Button>
        </div>
      ) : !status?.configured ? (
        <div className="p-4">
          <p className="text-sm font-medium">{t("Outlook is not available in this release.")}</p>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">{t("Open Meeting settings for simple setup help.")}</p>
          <Button type="button" size="sm" variant="outline" className="mt-3" onClick={onOpenSettings}>{t("Open settings")}</Button>
        </div>
      ) : status.authorizationPending ? (
        <div className="p-4">
          <p className="text-sm font-medium">{t("Finish Outlook sign-in in your browser.")}</p>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">{t("Today's events stay hidden until Microsoft confirms which account is connected.")}</p>
          <Button type="button" size="sm" variant="outline" className="mt-3" onClick={onOpenSettings}>{t("View connection status")}</Button>
        </div>
      ) : status.reauthRequired ? (
        <div className="p-4" role="alert">
          <p className="text-sm font-medium">{t("Reconnect Outlook to continue.")}</p>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">{t("Microsoft needs you to sign in again. Your saved meetings remain unchanged.")}</p>
          <Button type="button" size="sm" variant="outline" className="mt-3" onClick={onOpenSettings}>{t("Reconnect in Settings")}</Button>
        </div>
      ) : !status.connected ? (
        <div className="p-4">
          <p className="text-sm font-medium">{t("Connect Outlook to use meeting details.")}</p>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">{t("Scriber requests read-only calendar access and never receives your Microsoft password.")}</p>
          <Button type="button" size="sm" variant="outline" className="mt-3" onClick={onOpenSettings}>{t("Connect in Settings")}</Button>
        </div>
      ) : eventsLoading ? (
        <div className="grid gap-2 p-4" role="status" aria-label={t("Loading today's Outlook meetings")}>
          {[0, 1].map((item) => <div key={item} className="h-[70px] animate-pulse rounded-xl bg-muted/70 motion-reduce:animate-none" />)}
        </div>
      ) : eventsError && !events ? (
        <div className="p-4" role="alert">
          <p className="text-sm font-medium text-destructive">{t("Today's meetings could not be loaded.")}</p>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">{t("Your existing meeting setup is unchanged. Refresh the calendar to try again.")}</p>
          <Button type="button" size="sm" variant="outline" className="mt-3" disabled={refreshing} onClick={onRefresh}>{t("Refresh calendar")}</Button>
        </div>
      ) : (events?.items.length ?? 0) === 0 ? (
        <div className="p-4">
          <p className="text-sm font-medium">{t("No Outlook meetings today.")}</p>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">{t("You can still start an unlinked meeting and enter its title yourself.")}</p>
        </div>
      ) : (
        <div className="p-3">
          <div className="max-h-72 space-y-2 overflow-y-auto pr-1" role="list" aria-label={t("Today's Outlook meetings")}>
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
                      <span className="block truncate text-sm font-semibold">{event.subject || t("Untitled Outlook meeting")}</span>
                      <span className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
                        <span className="inline-flex items-center gap-1"><Clock3 className="h-3.5 w-3.5" />{event.isAllDay ? t("All day") : formatEventTime(event.start_at, event.end_at, formatDate, t)}</span>
                        <span className="inline-flex items-center gap-1"><Users className="h-3.5 w-3.5" />{t("{{count}} people", { count: formatNumber(eventContactCount(event)) })}</span>
                      </span>
                    </span>
                  </button>
                  {selected && (
                    <div className="border-t border-border/60 px-3 pb-3 pt-2.5">
                      <div className="flex items-center justify-between gap-3">
                        <p className="text-[11px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">{t("Participants")}</p>
                        <button type="button" className="text-[11px] font-medium text-muted-foreground hover:text-foreground" onClick={() => onSelect(null)}>{t("Use no calendar event")}</button>
                      </div>
                      <div className="mt-1 max-h-48 divide-y divide-border/50 overflow-y-auto pr-1">
                        {event.organizer && <ContactLine name={event.organizer.name} address={event.organizer.address} label={event.organizer.isCurrentUser ? t("You · organizer") : t("Organizer")} />}
                        {visibleParticipants(event).map((participant) => (
                          <ContactLine
                            key={participant.participantId || participant.address}
                            name={participant.name}
                            address={participant.address}
                            label={participantLabel(participant, t)}
                          />
                        ))}
                      </div>
                      {(event.location || event.join_url) && (
                        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-border/50 pt-2.5 text-xs text-muted-foreground">
                          {event.location && <span className="inline-flex min-w-0 items-center gap-1.5"><MapPin className="h-3.5 w-3.5 shrink-0" /><span className="truncate">{event.location}</span></span>}
                          {event.join_url && (
                            <button type="button" className="inline-flex items-center gap-1.5 font-medium text-primary hover:underline" onClick={() => void openMeetingUrl(event.join_url)}>
                              <ExternalLink className="h-3.5 w-3.5" />{t("Open online meeting")}
                            </button>
                          )}
                        </div>
                      )}
                      {event.participants.length === 0 && !event.organizer && (
                        <p className="mt-2 flex items-center gap-1.5 text-xs text-muted-foreground"><Mail className="h-3.5 w-3.5" />{t("No participant addresses are stored for this event.")}</p>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
          {events?.truncated && <p className="px-1 pt-2 text-[11px] leading-4 text-muted-foreground">{t("Only the first calendar entries for today are shown. Refresh after Outlook changes to get the latest list.")}</p>}
          {!selectedEvent && <p className="px-1 pt-2 text-[11px] leading-4 text-muted-foreground">{t("Select an event to copy its title and keep its participants with the meeting.")}</p>}
        </div>
      )}
    </section>
  );
}

import { getLocaleTag, translateNow } from "@/i18n";

export interface TranscriptHistoryPeriod {
  key: "today" | "last-week" | "last-month" | "older";
  label: string;
}

const DAY_MS = 24 * 60 * 60 * 1000;

function startOfLocalDay(value: Date): Date {
  return new Date(value.getFullYear(), value.getMonth(), value.getDate());
}

function localCalendarDayNumber(value: Date): number {
  return Date.UTC(value.getFullYear(), value.getMonth(), value.getDate()) / DAY_MS;
}

export function transcriptHistoryPeriod(
  createdAt?: string,
  now: Date = new Date(),
): TranscriptHistoryPeriod {
  const created = createdAt ? new Date(createdAt) : new Date(Number.NaN);
  if (Number.isNaN(created.getTime())) {
    return { key: "older", label: translateNow("Older") };
  }

  const today = startOfLocalDay(now);
  const createdDay = startOfLocalDay(created);
  const ageInDays = Math.max(0, localCalendarDayNumber(today) - localCalendarDayNumber(createdDay));

  if (ageInDays === 0) {
    return { key: "today", label: translateNow("Today") };
  }
  if (ageInDays <= 7) {
    return { key: "last-week", label: translateNow("Last week") };
  }
  if (ageInDays <= 30) {
    return { key: "last-month", label: translateNow("Last month") };
  }
  return { key: "older", label: translateNow("Older") };
}

export function recordingTimeLabel(createdAt?: string, fallback = ""): string {
  const created = createdAt ? new Date(createdAt) : new Date(Number.NaN);
  if (!Number.isNaN(created.getTime())) {
    return new Intl.DateTimeFormat(getLocaleTag(), {
      hour: "2-digit",
      minute: "2-digit",
    }).format(created);
  }
  const match = fallback.match(/(?:^|,\s*)(\d{1,2}:\d{2})(?:\s|$)/);
  return match?.[1] || (fallback ? translateNow(fallback) : translateNow("Time unavailable"));
}

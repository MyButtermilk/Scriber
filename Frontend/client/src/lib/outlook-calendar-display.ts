function sameLocalDate(left: Date, right: Date): boolean {
  return left.getFullYear() === right.getFullYear()
    && left.getMonth() === right.getMonth()
    && left.getDate() === right.getDate();
}

/** Formats one successful calendar sync without exposing transport details. */
export function formatOutlookSyncMoment(
  value: string,
  now = new Date(),
  locale?: string,
): string | null {
  const syncedAt = new Date(value);
  if (!value || Number.isNaN(syncedAt.getTime())) return null;

  const time = new Intl.DateTimeFormat(locale, {
    hour: "2-digit",
    minute: "2-digit",
  }).format(syncedAt);
  if (sameLocalDate(syncedAt, now)) return `today at ${time}`;

  const yesterday = new Date(now);
  yesterday.setDate(yesterday.getDate() - 1);
  if (sameLocalDate(syncedAt, yesterday)) return `yesterday at ${time}`;

  const date = new Intl.DateTimeFormat(locale, {
    day: "numeric",
    month: "short",
    ...(syncedAt.getFullYear() === now.getFullYear() ? {} : { year: "numeric" as const }),
  }).format(syncedAt);
  return `${date} at ${time}`;
}

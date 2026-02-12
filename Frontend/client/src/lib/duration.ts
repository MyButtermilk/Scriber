const UNKNOWN_DURATION_VALUES = new Set([
  "",
  "--",
  "--:--",
  "-:--",
  "—",
  "n/a",
  "na",
  "unknown",
]);

function formatSeconds(totalSeconds: number): string {
  const safeSeconds = Math.max(0, Math.floor(totalSeconds));
  const hours = Math.floor(safeSeconds / 3600);
  const minutes = Math.floor((safeSeconds % 3600) / 60);
  const seconds = safeSeconds % 60;

  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  }
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function parseIso8601Duration(raw: string): number | null {
  const match = raw.match(/^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$/i);
  if (!match) return null;

  const hours = Number(match[1] || 0);
  const minutes = Number(match[2] || 0);
  const seconds = Number(match[3] || 0);
  if ([hours, minutes, seconds].some((value) => Number.isNaN(value))) {
    return null;
  }

  return hours * 3600 + minutes * 60 + seconds;
}

export function formatDurationLikeYoutube(value?: string | number | null): string {
  if (value == null) return "—";

  if (typeof value === "number" && Number.isFinite(value)) {
    return formatSeconds(value);
  }

  const raw = String(value).trim();
  if (!raw) return "—";
  if (UNKNOWN_DURATION_VALUES.has(raw.toLowerCase())) return "—";

  if (/^\d+$/.test(raw)) {
    return formatSeconds(Number(raw));
  }

  const isoSeconds = parseIso8601Duration(raw);
  if (isoSeconds != null) {
    return formatSeconds(isoSeconds);
  }

  const parts = raw.split(":").map((part) => part.trim());
  if (parts.length === 2 && parts.every((part) => /^\d+$/.test(part))) {
    const minutes = Number(parts[0]);
    const seconds = Number(parts[1]);
    if (seconds >= 60) {
      return formatSeconds(minutes * 60 + seconds);
    }
    return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  }

  if (parts.length === 3 && parts.every((part) => /^\d+$/.test(part))) {
    const hours = Number(parts[0]);
    const minutes = Number(parts[1]);
    const seconds = Number(parts[2]);
    return formatSeconds(hours * 3600 + minutes * 60 + seconds);
  }

  return raw;
}

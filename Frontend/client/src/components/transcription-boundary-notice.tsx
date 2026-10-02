import { useI18n } from "@/i18n";
import type { TranscriptionBoundaryWarning } from "@/lib/api-types";

function originalTime(milliseconds: number): string {
  const seconds = Math.floor(milliseconds / 1000);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = String(seconds % 60).padStart(2, "0");
  return hours > 0 ? `${hours}:${String(minutes).padStart(2, "0")}:${remainder}` : `${minutes}:${remainder}`;
}

export function TranscriptionBoundaryNotice({ warnings }: { warnings?: TranscriptionBoundaryWarning[] }) {
  const { t } = useI18n();
  const intervals = (Array.isArray(warnings) ? warnings : []).filter(
    (warning) =>
      warning &&
      Number.isFinite(warning.startMs) &&
      Number.isFinite(warning.endMs) &&
      warning.startMs >= 0 &&
      warning.endMs >= warning.startMs,
  );
  if (intervals.length === 0) return null;
  return (
    <div role="status" className="rounded-xl border border-border bg-muted/40 px-4 py-3 text-sm text-muted-foreground">
      <p>{t("Some joins between audio parts need review.")}</p>
      <p className="mt-1">
        {t("Check these times in the original recording:")}{" "}
        {intervals.map((warning, index) => (
          <span key={`${warning.leftPartIndex}-${warning.rightPartIndex}-${index}`}>
            {index > 0 ? ", " : ""}
            {originalTime(warning.startMs)}–{originalTime(warning.endMs)}
          </span>
        ))}
      </p>
    </div>
  );
}

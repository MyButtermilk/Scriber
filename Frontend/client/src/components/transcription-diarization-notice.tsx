import { useI18n } from "@/i18n";

export function TranscriptionDiarizationNotice({ code }: { code?: string | null }) {
  const { t } = useI18n();
  if (code !== "diarization_unavailable") return null;
  return (
    <p role="status" className="rounded-xl border border-border bg-muted/40 px-4 py-3 text-sm text-muted-foreground">
      {t("Azure speaker diarization was unavailable. The transcript was completed without speaker labels.")}
    </p>
  );
}

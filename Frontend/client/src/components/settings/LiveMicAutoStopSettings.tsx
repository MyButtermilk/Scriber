import { useId } from "react";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { useI18n } from "@/i18n";
import type { SettingsUpdatePayload } from "@/lib/api-types";

type AutoStopPatch = Pick<SettingsUpdatePayload, "micAutoStopEnabled" | "micAutoStopSilenceSeconds">;

export function LiveMicAutoStopSettings({
  enabled,
  silenceSeconds,
  saving,
  onChange,
}: {
  enabled: boolean;
  silenceSeconds: number;
  saving: boolean;
  onChange: (patch: AutoStopPatch) => void;
}) {
  const { t, formatNumber } = useI18n();
  const id = useId();

  return (
    <div className="space-y-3 py-2.5">
      <div className="grid gap-2.5 sm:grid-cols-[minmax(0,1fr)_minmax(150px,220px)] sm:items-center">
        <div className="min-w-0">
          <Label
            htmlFor={`${id}-enabled`}
            className="text-[13px] font-semibold leading-4 text-slate-950 dark:text-slate-100"
          >
            {t("Stop after silence")}
          </Label>
          <p id={`${id}-description`} className="mt-1 text-[12px] leading-[16px] text-slate-600 dark:text-slate-400">
            {t("Automatically stops Live Mic after you finish speaking. Applies to new recordings.")}
          </p>
        </div>
        <Switch
          id={`${id}-enabled`}
          checked={enabled}
          disabled={saving}
          aria-busy={saving}
          aria-describedby={`${id}-description`}
          className="sm:justify-self-end"
          onCheckedChange={(value) => onChange({ micAutoStopEnabled: value })}
        />
      </div>
      <div className="grid gap-2.5 sm:grid-cols-[minmax(0,1fr)_minmax(150px,220px)] sm:items-center">
        <Label htmlFor={`${id}-seconds`} className="text-[13px] font-semibold text-slate-950 dark:text-slate-100">
          {t("Silence duration")}
        </Label>
        <select
          id={`${id}-seconds`}
          value={silenceSeconds}
          disabled={!enabled || saving}
          aria-busy={saving}
          className="h-8 w-[220px] max-w-full rounded-md border border-input bg-background px-3 text-[12px] outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50 sm:justify-self-end"
          onChange={(event) => onChange({ micAutoStopSilenceSeconds: Number(event.target.value) })}
        >
          {Array.from({ length: 10 }, (_, index) => index + 1).map((seconds) => (
            <option key={seconds} value={seconds}>
              {seconds === 1 ? t("1 second") : t("{{seconds}} seconds", { seconds: formatNumber(seconds) })}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}

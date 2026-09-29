import { useId } from "react";
import { useI18n } from "@/i18n";
import type { OpenRouterRegion } from "@/lib/api-types";

export function OpenRouterRegionPicker({
  value,
  onValueChange,
}: {
  value: OpenRouterRegion;
  onValueChange: (value: OpenRouterRegion) => void;
}) {
  const { t } = useI18n();
  const id = useId();
  return (
    <div className="space-y-2.5">
      <label htmlFor={id} className="text-[13px] font-semibold text-slate-950 dark:text-slate-100">
        {t("OpenRouter data processing region")}
      </label>
      <select
        id={id}
        value={value}
        aria-describedby={`${id}-scope ${id}-availability`}
        className="h-9 w-full rounded-md border border-input bg-background px-3 text-[12px] outline-none focus-visible:ring-2 focus-visible:ring-ring"
        onChange={(event) => onValueChange(event.target.value as OpenRouterRegion)}
      >
        <option value="eu">{t("European Union (default)")}</option>
        <option value="us">{t("United States")}</option>
        <option value="global">{t("Global (no region restriction)")}</option>
      </select>
      <p id={`${id}-scope`} className="text-[12px] leading-4 text-slate-600 dark:text-slate-400">
        {t(
          "Applies to OpenRouter transcription, summaries, meeting analysis, and cloud cleanup, including OpenRouter fallbacks. Other providers keep their own settings.",
        )}
      </p>
      <p id={`${id}-availability`} className="text-[12px] leading-4 text-slate-600 dark:text-slate-400">
        {t(
          "EU and US routing require OpenRouter Business or Enterprise. Your API key and model IDs stay the same. Models unavailable in the selected region fail with HTTP 404; Scriber never switches to Global automatically.",
        )}
      </p>
    </div>
  );
}

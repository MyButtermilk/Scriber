import { Languages } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { useI18n } from "@/i18n";

interface LanguageToggleProps {
  compact?: boolean;
  className?: string;
}

export function LanguageToggle({ compact = false, className }: LanguageToggleProps) {
  const { locale, setLocale, toggleLocale, t } = useI18n();
  const switchLabel = locale === "de" ? t("Switch interface to English") : t("Switch interface to German");

  if (!compact) {
    return (
      <div
        className={cn("language-toggle flex h-11 min-w-0 flex-1 items-center gap-1", className)}
        role="group"
        aria-label={t("Language")}
      >
        {(["de", "en"] as const).map((option) => {
          const selected = locale === option;
          const optionLabel = option === "de" ? "Deutsch" : "English";
          const switchOptionLabel =
            option === "de" ? t("Switch interface to German") : t("Switch interface to English");
          const ariaLabel = selected ? optionLabel : switchOptionLabel;

          return (
            <button
              key={option}
              type="button"
              className={cn(
                "language-toggle__option h-11 min-w-0 flex-1 rounded-lg border border-transparent px-2 text-xs font-semibold outline-none",
                selected ? "is-active text-foreground" : "text-muted-foreground",
              )}
              onClick={() => setLocale(option)}
              aria-label={ariaLabel}
              aria-pressed={selected}
              title={ariaLabel}
            >
              {optionLabel}
            </button>
          );
        })}
      </div>
    );
  }

  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      className={cn("min-h-[44px] min-w-[44px]", className)}
      onClick={toggleLocale}
      aria-label={switchLabel}
      title={switchLabel}
    >
      <Languages className="h-4 w-4 shrink-0" aria-hidden="true" />
      <span className="sr-only">{switchLabel}</span>
    </Button>
  );
}

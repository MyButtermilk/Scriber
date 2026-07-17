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
  const switchLabel = locale === "de"
    ? t("Switch interface to English")
    : t("Switch interface to German");

  if (!compact) {
    return (
      <div
        className={cn(
          "flex h-10 items-center gap-1 rounded-xl border border-border/60 bg-background/45 p-1 shadow-[inset_0_1px_0_hsl(var(--background)/0.7)]",
          className,
        )}
        role="group"
        aria-label={t("Language")}
      >
        <span className="flex h-8 w-8 shrink-0 items-center justify-center text-muted-foreground" aria-hidden="true">
          <Languages className="h-4 w-4" />
        </span>
        {(["de", "en"] as const).map((option) => {
          const selected = locale === option;
          const optionLabel = option === "de" ? "Deutsch" : "English";
          const switchOptionLabel = option === "de"
            ? t("Switch interface to German")
            : t("Switch interface to English");
          const ariaLabel = selected ? optionLabel : switchOptionLabel;

          return (
            <button
              key={option}
              type="button"
              className={cn(
                "h-8 min-w-0 flex-1 rounded-lg px-2 text-xs font-semibold outline-none transition-[background-color,color,box-shadow] duration-[var(--duration-quick)] ease-[var(--ease-smooth-out)] focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-1 focus-visible:ring-offset-sidebar motion-reduce:transition-none",
                selected
                  ? "bg-background text-foreground shadow-sm ring-1 ring-border/60"
                  : "text-muted-foreground hover:bg-background/55 hover:text-foreground",
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

import { Languages } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { useI18n } from "@/i18n";

interface LanguageToggleProps {
  compact?: boolean;
  className?: string;
}

export function LanguageToggle({ compact = false, className }: LanguageToggleProps) {
  const { locale, toggleLocale, t } = useI18n();
  const switchLabel = locale === "de"
    ? t("Switch interface to English")
    : t("Switch interface to German");

  return (
    <Button
      type="button"
      variant="ghost"
      size={compact ? "icon" : "sm"}
      className={cn(
        compact ? "min-h-[44px] min-w-[44px]" : "h-9 justify-start gap-2 px-2.5 text-muted-foreground",
        className,
      )}
      onClick={toggleLocale}
      aria-label={switchLabel}
      title={switchLabel}
    >
      <Languages className="h-4 w-4 shrink-0" aria-hidden="true" />
      {compact ? (
        <span className="sr-only">{switchLabel}</span>
      ) : (
        <>
          <span className="flex-1 text-left">{locale === "de" ? "Deutsch" : "English"}</span>
          <span className="rounded-md border border-border/70 bg-background/70 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-foreground">
            {locale.toUpperCase()}
          </span>
        </>
      )}
    </Button>
  );
}

import { Search } from "lucide-react";
import { useEffect, useState } from "react";
import { useI18n } from "@/i18n";

interface SidebarSearchProps {
  placeholder?: string;
  onOpenCommandPalette?: () => void;
}

export function SidebarSearch({ placeholder = "Search", onOpenCommandPalette }: SidebarSearchProps) {
  const { locale, t } = useI18n();
  const [isMac, setIsMac] = useState(false);

  useEffect(() => {
    setIsMac(navigator.platform.toLowerCase().includes("mac"));
  }, []);

  return (
    <button
      type="button"
      className={[
        "flex w-full cursor-pointer items-center gap-2 rounded-[10px] border border-border/90",
        "bg-background/60 px-3 py-2 text-left shadow-none",
        "transition-[background-color,border-color] duration-[var(--duration-quick)] ease-[var(--ease-smooth-out)]",
        "hover:border-foreground/20 hover:bg-background/90",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
        "focus-visible:ring-offset-sidebar motion-reduce:transition-none dark:bg-background/70 dark:hover:bg-background/90",
      ].join(" ")}
      aria-label={t("Open command palette")}
      aria-keyshortcuts="Control+K Meta+K"
      onClick={() => onOpenCommandPalette?.()}
    >
      <Search className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      <span className="min-w-0 flex-1 truncate text-ui-body-sm text-muted-foreground">{t(placeholder)}</span>
      <kbd
        className="inline-flex min-w-9 shrink-0 items-center justify-center rounded-md border border-border/90 bg-background/90 px-1.5 py-0.5 text-ui-micro font-medium leading-none text-muted-foreground"
      >
        {isMac ? "⌘K" : locale === "de" ? "Strg+K" : "Ctrl+K"}
      </kbd>
    </button>
  );
}

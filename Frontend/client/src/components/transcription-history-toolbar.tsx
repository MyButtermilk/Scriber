import { useCallback, useEffect, useLayoutEffect, useRef } from "react";
import { LayoutGrid, LayoutList } from "lucide-react";

import { TranscriptHistorySearch } from "@/components/transcript-history-search";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { cn } from "@/lib/utils";
import { useI18n } from "@/i18n";

import type { TranscriptHistoryViewMode } from "@/hooks/use-transcript-history-panel-state";

interface TranscriptionHistoryToolbarProps {
  title: string;
  description: string;
  total: number;
  itemLabel: string;
  searchValue: string;
  onSearchChange: (value: string) => void;
  searchPlaceholder: string;
  searchAriaLabel: string;
  clearSearchLabel: string;
  viewMode: TranscriptHistoryViewMode;
  onViewModeChange: (value: TranscriptHistoryViewMode) => void;
  className?: string;
}

export function TranscriptionHistoryToolbar({
  title,
  description,
  total,
  itemLabel,
  searchValue,
  onSearchChange,
  searchPlaceholder,
  searchAriaLabel,
  clearSearchLabel,
  viewMode,
  onViewModeChange,
  className,
}: TranscriptionHistoryToolbarProps) {
  const { formatNumber, t } = useI18n();
  const formattedTotal = formatNumber(total);
  const tabsRef = useRef<HTMLDivElement>(null);
  const pillRef = useRef<HTMLSpanElement>(null);
  const indicatorReadyRef = useRef(false);
  const moveIndicator = useCallback((animate: boolean) => {
    const tabs = tabsRef.current;
    const pill = pillRef.current;
    const activeTab = tabs?.querySelector<HTMLElement>('.t-tab[data-state="on"]');
    if (!tabs || !pill || !activeTab) return;

    if (!animate) {
      const previousTransition = pill.style.transition;
      pill.style.transition = "none";
      pill.style.transform = `translateX(${activeTab.offsetLeft}px)`;
      pill.style.width = `${activeTab.offsetWidth}px`;
      void pill.offsetWidth;
      pill.style.transition = previousTransition;
      return;
    }

    pill.style.transform = `translateX(${activeTab.offsetLeft}px)`;
    pill.style.width = `${activeTab.offsetWidth}px`;
  }, []);

  useLayoutEffect(() => {
    moveIndicator(indicatorReadyRef.current);
    indicatorReadyRef.current = true;
  }, [moveIndicator, viewMode]);

  useEffect(() => {
    const tabs = tabsRef.current;
    if (!tabs) return;
    const updateWithoutAnimation = () => moveIndicator(false);
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(updateWithoutAnimation);
    observer?.observe(tabs);
    tabs.querySelectorAll<HTMLElement>(".t-tab").forEach((tab) => observer?.observe(tab));
    window.addEventListener("resize", updateWithoutAnimation);
    return () => {
      observer?.disconnect();
      window.removeEventListener("resize", updateWithoutAnimation);
    };
  }, [moveIndicator]);

  return (
    <header
      className={cn(
        "transcription-history-toolbar flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between",
        className,
      )}
    >
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2.5">
          <h2 className="font-heading text-[20px] font-semibold tracking-[-0.02em] text-foreground">{title}</h2>
          <span
            className="transcription-history-count inline-flex h-6 min-w-6 items-center justify-center rounded-[8px] px-2 font-mono text-ui-micro font-semibold tabular-nums text-muted-foreground"
            aria-label={`${formattedTotal} ${itemLabel}`}
          >
            {formattedTotal}
          </span>
        </div>
        <p className="mt-1 max-w-[58ch] text-pretty text-[12px] leading-4 text-muted-foreground">{description}</p>
      </div>

      <div className="transcription-history-controls flex w-full min-w-0 items-center gap-2 lg:w-auto">
        <TranscriptHistorySearch
          value={searchValue}
          onChange={onSearchChange}
          placeholder={searchPlaceholder}
          ariaLabel={searchAriaLabel}
          clearLabel={clearSearchLabel}
          className="lg:w-[320px] min-[1280px]:w-[380px]"
        />
        <ToggleGroup
          ref={tabsRef}
          type="single"
          value={viewMode}
          onValueChange={(value) => {
            if (value === "list" || value === "grid") onViewModeChange(value);
          }}
          className="t-tabs transcription-view-toggle shrink-0 rounded-[12px] p-1"
          aria-label={t("Transcript history layout")}
        >
          <span ref={pillRef} className="t-tabs-pill" aria-hidden="true" />
          <ToggleGroupItem
            value="list"
            aria-label={t("List view")}
            className="t-tab h-10 w-10 rounded-[9px] p-0 active:scale-[0.96]"
          >
            <LayoutList className="h-4 w-4" aria-hidden="true" />
          </ToggleGroupItem>
          <ToggleGroupItem
            value="grid"
            aria-label={t("Grid view")}
            className="t-tab h-10 w-10 rounded-[9px] p-0 active:scale-[0.96]"
          >
            <LayoutGrid className="h-4 w-4" aria-hidden="true" />
          </ToggleGroupItem>
        </ToggleGroup>
      </div>
    </header>
  );
}

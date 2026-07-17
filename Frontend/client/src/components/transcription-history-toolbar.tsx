import { LayoutGrid, LayoutList } from "lucide-react";

import { TranscriptHistorySearch } from "@/components/transcript-history-search";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { cn } from "@/lib/utils";
import { useI18n } from "@/i18n";

type HistoryViewMode = "list" | "grid";

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
  viewMode: HistoryViewMode;
  onViewModeChange: (value: HistoryViewMode) => void;
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
  return (
    <header
      className={cn(
        "transcription-history-toolbar flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between",
        className,
      )}
    >
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2.5">
          <h2 className="font-heading text-[20px] font-semibold tracking-[-0.02em] text-foreground">
            {title}
          </h2>
          <span
            className="transcription-history-count inline-flex h-6 min-w-6 items-center justify-center rounded-[8px] px-2 font-mono text-[10.5px] font-semibold tabular-nums text-muted-foreground"
            aria-label={`${formattedTotal} ${itemLabel}`}
          >
            {formattedTotal}
          </span>
        </div>
        <p className="mt-1 max-w-[58ch] text-pretty text-[12px] leading-4 text-muted-foreground">
          {description}
        </p>
      </div>

      <div className="transcription-history-controls flex w-full min-w-0 items-center gap-2 sm:w-auto">
        <TranscriptHistorySearch
          value={searchValue}
          onChange={onSearchChange}
          placeholder={searchPlaceholder}
          ariaLabel={searchAriaLabel}
          clearLabel={clearSearchLabel}
          className="sm:w-[320px] lg:w-[380px]"
        />
        <ToggleGroup
          type="single"
          value={viewMode}
          onValueChange={(value) => {
            if (value === "list" || value === "grid") onViewModeChange(value);
          }}
          className="transcription-view-toggle shrink-0 rounded-[12px] p-1"
          aria-label={t("Transcript history layout")}
        >
          <ToggleGroupItem
            value="list"
            aria-label={t("List view")}
            className="h-10 w-10 rounded-[9px] p-0 transition-[background-color,color,transform] duration-200 active:scale-[0.96]"
          >
            <LayoutList className="h-4 w-4" aria-hidden="true" />
          </ToggleGroupItem>
          <ToggleGroupItem
            value="grid"
            aria-label={t("Grid view")}
            className="h-10 w-10 rounded-[9px] p-0 transition-[background-color,color,transform] duration-200 active:scale-[0.96]"
          >
            <LayoutGrid className="h-4 w-4" aria-hidden="true" />
          </ToggleGroupItem>
        </ToggleGroup>
      </div>
    </header>
  );
}

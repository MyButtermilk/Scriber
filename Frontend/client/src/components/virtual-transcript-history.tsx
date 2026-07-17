import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Loader2 } from "lucide-react";
import {
  calculateHistoryGridColumns,
  calculateHistoryRowTranslateY,
  calculateHistoryScrollMargin,
} from "@/lib/virtual-transcript-history-layout";
import { cn } from "@/lib/utils";
import { useI18n } from "@/i18n";

type ViewMode = "list" | "grid";

export interface TranscriptHistoryGroup {
  key: string;
  label: string;
}

interface VirtualTranscriptHistoryProps<TItem> {
  items: TItem[];
  viewMode: ViewMode;
  renderItem: (item: TItem, index: number) => ReactNode;
  getItemKey: (item: TItem, index: number) => string | number;
  getItemGroup?: (item: TItem, index: number) => TranscriptHistoryGroup;
  renderGroupHeader?: (group: TranscriptHistoryGroup, itemCount: number) => ReactNode;
  hasMore?: boolean;
  isLoadingMore?: boolean;
  onLoadMore?: () => void | Promise<unknown>;
  className?: string;
  gridClassName?: string;
  estimateListRowHeight?: number;
  estimateGridRowHeight?: number;
  estimateGroupHeaderHeight?: number;
}

type VirtualItem<TItem> = {
  item: TItem;
  index: number;
};

type VirtualRow<TItem> =
  | {
      kind: "group";
      group: TranscriptHistoryGroup;
      itemCount: number;
    }
  | {
      kind: "items";
      items: VirtualItem<TItem>[];
    };

function defaultGroupHeader(group: TranscriptHistoryGroup, itemCount: number) {
  return (
    <div className="flex items-baseline gap-2 px-1 pb-3 pt-2">
      <h3 className="font-heading text-[13px] font-semibold tracking-[-0.01em] text-foreground">
        {group.label}
      </h3>
      <span className="text-[11px] tabular-nums text-muted-foreground">{itemCount}</span>
    </div>
  );
}

export function VirtualTranscriptHistory<TItem>({
  items,
  viewMode,
  renderItem,
  getItemKey,
  getItemGroup,
  renderGroupHeader = defaultGroupHeader,
  hasMore = false,
  isLoadingMore = false,
  onLoadMore,
  className,
  gridClassName,
  estimateListRowHeight = 116,
  estimateGridRowHeight = 252,
  estimateGroupHeaderHeight = 48,
}: VirtualTranscriptHistoryProps<TItem>) {
  const { t } = useI18n();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const loadMoreRef = useRef<HTMLDivElement | null>(null);
  const loadInFlightRef = useRef(false);
  const [containerWidth, setContainerWidth] = useState(0);
  const [scrollElement, setScrollElement] = useState<HTMLDivElement | null>(null);
  const [scrollMargin, setScrollMargin] = useState(0);

  const gridColumns = useMemo(
    () => (viewMode === "grid" ? calculateHistoryGridColumns(containerWidth) : 1),
    [containerWidth, viewMode],
  );

  const rows = useMemo<Array<VirtualRow<TItem>>>(() => {
    const indexedItems = items.map((item, index) => ({ item, index }));
    if (!getItemGroup) {
      const result: Array<VirtualRow<TItem>> = [];
      for (let index = 0; index < indexedItems.length; index += gridColumns) {
        result.push({
          kind: "items",
          items: indexedItems.slice(index, index + gridColumns),
        });
      }
      return result;
    }

    const grouped: Array<{
      group: TranscriptHistoryGroup;
      items: VirtualItem<TItem>[];
    }> = [];
    for (const entry of indexedItems) {
      const group = getItemGroup(entry.item, entry.index);
      const current = grouped[grouped.length - 1];
      if (!current || current.group.key !== group.key) {
        grouped.push({ group, items: [entry] });
      } else {
        current.items.push(entry);
      }
    }

    const result: Array<VirtualRow<TItem>> = [];
    for (const section of grouped) {
      result.push({
        kind: "group",
        group: section.group,
        itemCount: section.items.length,
      });
      for (let index = 0; index < section.items.length; index += gridColumns) {
        result.push({
          kind: "items",
          items: section.items.slice(index, index + gridColumns),
        });
      }
    }
    return result;
  }, [getItemGroup, gridColumns, items]);

  useLayoutEffect(() => {
    const element = containerRef.current;
    if (!element) return;
    const nextScrollElement = element.closest<HTMLDivElement>("[data-app-scroll-container]");

    const updateLayoutMetrics = () => {
      const containerRect = element.getBoundingClientRect();
      setScrollElement(nextScrollElement);
      setContainerWidth(containerRect.width);
      setScrollMargin(
        nextScrollElement
          ? calculateHistoryScrollMargin(
              containerRect.top,
              nextScrollElement.getBoundingClientRect().top,
              nextScrollElement.scrollTop,
            )
          : 0,
      );
    };

    updateLayoutMetrics();
    window.addEventListener("resize", updateLayoutMetrics);
    const observer = new ResizeObserver(updateLayoutMetrics);

    // A shared outer scroller means the list origin moves whenever content
    // before it changes height. Observe the ancestor chain so that origin is
    // refreshed without doing layout work on every scroll event.
    let layoutElement: HTMLElement | null = element;
    while (layoutElement) {
      observer.observe(layoutElement);
      if (layoutElement === nextScrollElement) break;
      layoutElement = layoutElement.parentElement;
    }

    return () => {
      window.removeEventListener("resize", updateLayoutMetrics);
      observer.disconnect();
    };
  }, []);

  const virtualizer = useVirtualizer<HTMLDivElement, HTMLDivElement>({
    count: rows.length,
    getScrollElement: () => scrollElement,
    estimateSize: (index) => {
      const row = rows[index];
      if (row?.kind === "group") return estimateGroupHeaderHeight;
      return viewMode === "grid" ? estimateGridRowHeight : estimateListRowHeight;
    },
    overscan: 6,
    scrollMargin,
    getItemKey: (index) => {
      const row = rows[index];
      if (!row) return index;
      if (row.kind === "group") return `group-${row.group.key}`;
      return row.items.map(({ item, index: itemIndex }) => getItemKey(item, itemIndex)).join("|");
    },
  });

  useEffect(() => {
    virtualizer.measure();
  }, [gridColumns, items.length, rows.length, viewMode, virtualizer]);

  const loadNextPage = useCallback(() => {
    if (!hasMore || isLoadingMore || loadInFlightRef.current) return;
    loadInFlightRef.current = true;
    try {
      const result = onLoadMore?.();
      if (result && typeof result === "object" && "then" in result) {
        void Promise.resolve(result)
          .catch((error) => {
            console.debug("Loading the next transcript page failed.", error);
          })
          .finally(() => {
            loadInFlightRef.current = false;
          });
      } else {
        loadInFlightRef.current = false;
      }
    } catch (error) {
      loadInFlightRef.current = false;
      console.debug("Loading the next transcript page failed.", error);
    }
  }, [hasMore, isLoadingMore, onLoadMore]);

  useEffect(() => {
    if (!isLoadingMore) {
      loadInFlightRef.current = false;
    }
  }, [isLoadingMore, items.length]);

  useEffect(() => {
    const element = loadMoreRef.current;
    if (!element || !hasMore) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          loadNextPage();
        }
      },
      { root: scrollElement, rootMargin: "800px 0px" },
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, [hasMore, loadNextPage, scrollElement]);

  const virtualRows = virtualizer.getVirtualItems();
  const lastVirtualIndex = virtualRows.length ? virtualRows[virtualRows.length - 1].index : -1;

  useEffect(() => {
    if (lastVirtualIndex >= rows.length - 3) {
      loadNextPage();
    }
  }, [lastVirtualIndex, loadNextPage, rows.length]);

  return (
    <div
      ref={containerRef}
      className={cn("w-full", className)}
      data-history-virtualized="true"
      data-history-view={viewMode}
    >
      <div
        style={{
          height: `${virtualizer.getTotalSize()}px`,
          position: "relative",
          width: "100%",
        }}
      >
        {virtualRows.map((virtualRow) => {
          const row = rows[virtualRow.index];
          if (!row) return null;
          return (
            <div
              key={virtualRow.key}
              ref={virtualizer.measureElement}
              data-index={virtualRow.index}
              style={{
                left: 0,
                position: "absolute",
                top: 0,
                transform: `translate3d(0, ${calculateHistoryRowTranslateY(virtualRow.start, scrollMargin)}px, 0)`,
                width: "100%",
              }}
            >
              {row.kind === "group" ? (
                renderGroupHeader(row.group, row.itemCount)
              ) : viewMode === "grid" ? (
                <div
                  className={cn("grid items-stretch gap-4 pb-4", gridClassName)}
                  style={{ gridTemplateColumns: `repeat(${gridColumns}, minmax(0, 1fr))` }}
                >
                  {row.items.map(({ item, index }) => (
                    <div key={getItemKey(item, index)} className="h-full min-w-0">
                      {renderItem(item, index)}
                    </div>
                  ))}
                </div>
              ) : (
                row.items.map(({ item, index }) => (
                  <div key={getItemKey(item, index)} className="pb-4 [&>*]:!mb-0">
                    {renderItem(item, index)}
                  </div>
                ))
              )}
            </div>
          );
        })}
      </div>

      <div ref={loadMoreRef} className="flex h-12 items-center justify-center" aria-live="polite">
        {isLoadingMore && (
          <>
            <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" aria-hidden="true" />
            <span className="sr-only">{t("Loading more transcripts")}</span>
          </>
        )}
      </div>
    </div>
  );
}

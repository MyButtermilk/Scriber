import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { AnimatePresence, LayoutGroup, motion, useReducedMotion } from "motion/react";
import { Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";

type ViewMode = "list" | "grid";

interface VirtualTranscriptHistoryProps<TItem> {
  items: TItem[];
  viewMode: ViewMode;
  renderItem: (item: TItem, index: number) => ReactNode;
  getItemKey: (item: TItem, index: number) => string | number;
  hasMore?: boolean;
  isLoadingMore?: boolean;
  onLoadMore?: () => void | Promise<unknown>;
  className?: string;
  gridClassName?: string;
  estimateListRowHeight?: number;
  estimateGridRowHeight?: number;
}

type VirtualRow<TItem> = Array<{
  item: TItem;
  index: number;
}>;

const GRID_GAP_PX = 16;
const DEFAULT_MIN_GRID_COLUMN_WIDTH = 210;
const HISTORY_LAYOUT_TRANSITION = {
  type: "spring",
  stiffness: 520,
  damping: 44,
  mass: 0.82,
} as const;
const HISTORY_ITEM_TRANSITION = {
  duration: 0.18,
  ease: [0.16, 1, 0.3, 1],
} as const;

function calculateGridColumns(width: number) {
  if (width <= 0) return 1;
  return Math.max(1, Math.floor((width + GRID_GAP_PX) / (DEFAULT_MIN_GRID_COLUMN_WIDTH + GRID_GAP_PX)));
}

export function VirtualTranscriptHistory<TItem>({
  items,
  viewMode,
  renderItem,
  getItemKey,
  hasMore = false,
  isLoadingMore = false,
  onLoadMore,
  className,
  gridClassName,
  estimateListRowHeight = 116,
  estimateGridRowHeight = 252,
}: VirtualTranscriptHistoryProps<TItem>) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const loadMoreRef = useRef<HTMLDivElement | null>(null);
  const loadInFlightRef = useRef(false);
  const prefersReducedMotion = useReducedMotion();
  const [containerWidth, setContainerWidth] = useState(0);
  const [scrollElement, setScrollElement] = useState<HTMLDivElement | null>(null);

  const gridColumns = useMemo(
    () => (viewMode === "grid" ? calculateGridColumns(containerWidth) : 1),
    [containerWidth, viewMode],
  );

  const rows = useMemo<Array<VirtualRow<TItem>>>(() => {
    if (viewMode === "list") {
      return items.map((item, index) => [{ item, index }]);
    }

    const nextRows: Array<VirtualRow<TItem>> = [];
    for (let index = 0; index < items.length; index += gridColumns) {
      nextRows.push(
        items.slice(index, index + gridColumns).map((item, itemOffset) => ({
          item,
          index: index + itemOffset,
        })),
      );
    }
    return nextRows;
  }, [gridColumns, items, viewMode]);

  useLayoutEffect(() => {
    const updateLayoutMetrics = () => {
      const element = containerRef.current;
      if (!element) return;
      setScrollElement(element.closest<HTMLDivElement>("[data-app-scroll-container]"));
      const rect = element.getBoundingClientRect();
      setContainerWidth(rect.width);
    };

    updateLayoutMetrics();
    window.addEventListener("resize", updateLayoutMetrics);

    const observer = new ResizeObserver(updateLayoutMetrics);
    if (containerRef.current) {
      observer.observe(containerRef.current);
    }

    return () => {
      window.removeEventListener("resize", updateLayoutMetrics);
      observer.disconnect();
    };
  }, []);

  const virtualizer = useVirtualizer<HTMLDivElement, HTMLDivElement>({
    count: rows.length,
    getScrollElement: () => scrollElement,
    estimateSize: () => (viewMode === "grid" ? estimateGridRowHeight : estimateListRowHeight),
    overscan: 6,
    getItemKey: (index) => {
      const row = rows[index];
      if (!row?.length) return index;
      return row.map(({ item, index: itemIndex }) => getItemKey(item, itemIndex)).join("|");
    },
  });

  useEffect(() => {
    virtualizer.measure();
  }, [gridColumns, items.length, viewMode, virtualizer]);

  const loadNextPage = useCallback(() => {
    if (!hasMore || isLoadingMore || loadInFlightRef.current) return;
    loadInFlightRef.current = true;
    const result = onLoadMore?.();
    if (result && typeof result === "object" && "finally" in result) {
      void (result as Promise<unknown>).finally(() => {
        loadInFlightRef.current = false;
      });
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
      <LayoutGroup id={`transcript-history-${viewMode}`}>
        <div
          style={{
            height: `${virtualizer.getTotalSize()}px`,
            position: "relative",
            width: "100%",
          }}
        >
          <AnimatePresence initial={false} mode="popLayout">
            {virtualRows.map((virtualRow) => {
              const row = rows[virtualRow.index] ?? [];
              return (
                <motion.div
                  key={virtualRow.key}
                  ref={(node) => {
                    if (node) virtualizer.measureElement(node);
                  }}
                  data-index={virtualRow.index}
                  animate={{ opacity: 1, y: virtualRow.start }}
                  exit={prefersReducedMotion ? { opacity: 0 } : { opacity: 0, scale: 0.985 }}
                  initial={false}
                  transition={prefersReducedMotion ? { duration: 0 } : HISTORY_LAYOUT_TRANSITION}
                  style={{
                    left: 0,
                    position: "absolute",
                    top: 0,
                    width: "100%",
                  }}
                >
                  {viewMode === "grid" ? (
                    <div
                      className={cn("grid items-stretch gap-4 pb-4", gridClassName)}
                      style={{ gridTemplateColumns: `repeat(${gridColumns}, minmax(0, 1fr))` }}
                    >
                      <AnimatePresence initial={false} mode="popLayout">
                        {row.map(({ item, index }) => {
                          const itemKey = String(getItemKey(item, index));
                          return (
                            <motion.div
                              key={itemKey}
                              layout="position"
                              layoutId={`history-${viewMode}-${itemKey}`}
                              initial={prefersReducedMotion ? false : { opacity: 0, scale: 0.985, y: 8 }}
                              animate={{ opacity: 1, scale: 1, y: 0 }}
                              exit={prefersReducedMotion ? { opacity: 0 } : { opacity: 0, scale: 0.96, y: -6 }}
                              transition={prefersReducedMotion ? { duration: 0 } : HISTORY_ITEM_TRANSITION}
                              className="min-w-0"
                            >
                              {renderItem(item, index)}
                            </motion.div>
                          );
                        })}
                      </AnimatePresence>
                    </div>
                  ) : (
                    <AnimatePresence initial={false} mode="popLayout">
                      {row.map(({ item, index }) => {
                        const itemKey = String(getItemKey(item, index));
                        return (
                          <motion.div
                            key={itemKey}
                            layout="position"
                            layoutId={`history-${viewMode}-${itemKey}`}
                            initial={prefersReducedMotion ? false : { opacity: 0, scale: 0.99, y: 8 }}
                            animate={{ opacity: 1, scale: 1, y: 0 }}
                            exit={prefersReducedMotion ? { opacity: 0 } : { opacity: 0, scale: 0.97, y: -8 }}
                            transition={prefersReducedMotion ? { duration: 0 } : HISTORY_ITEM_TRANSITION}
                            className="pb-4 [&>*]:!mb-0"
                          >
                            {renderItem(item, index)}
                          </motion.div>
                        );
                      })}
                    </AnimatePresence>
                  )}
                </motion.div>
              );
            })}
          </AnimatePresence>
        </div>
      </LayoutGroup>

      <div ref={loadMoreRef} className="flex h-12 items-center justify-center" aria-live="polite">
        {isLoadingMore && (
          <>
            <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" aria-hidden="true" />
            <span className="sr-only">Loading more transcripts</span>
          </>
        )}
      </div>
    </div>
  );
}

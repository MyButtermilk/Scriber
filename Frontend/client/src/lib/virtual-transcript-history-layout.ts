const GRID_GAP_PX = 16;
const DEFAULT_MIN_GRID_COLUMN_WIDTH = 210;

export function calculateHistoryGridColumns(width: number) {
  if (width <= 0) return 1;
  return Math.max(
    1,
    Math.floor((width + GRID_GAP_PX) / (DEFAULT_MIN_GRID_COLUMN_WIDTH + GRID_GAP_PX)),
  );
}

export function calculateHistoryScrollMargin(
  containerTop: number,
  scrollElementTop: number,
  scrollOffset: number,
) {
  return Math.max(0, containerTop - scrollElementTop + scrollOffset);
}

export function calculateHistoryRowTranslateY(virtualRowStart: number, scrollMargin: number) {
  return virtualRowStart - scrollMargin;
}

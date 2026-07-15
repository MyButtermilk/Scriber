import assert from "node:assert/strict";
import test from "node:test";

import { Virtualizer } from "@tanstack/react-virtual";

import {
  calculateHistoryGridColumns,
  calculateHistoryRowTranslateY,
  calculateHistoryScrollMargin,
} from "./virtual-transcript-history-layout";

function assertVirtualRowsCoverViewport({
  count,
  rowHeight,
  scrollMargin,
  scrollOffset,
  viewportHeight,
}: {
  count: number;
  rowHeight: number;
  scrollMargin: number;
  scrollOffset: number;
  viewportHeight: number;
}) {
  const virtualizer = new Virtualizer<HTMLElement, HTMLDivElement>({
    count,
    estimateSize: () => rowHeight,
    getScrollElement: () => null,
    initialOffset: scrollOffset,
    initialRect: { width: 1_000, height: viewportHeight },
    observeElementOffset: () => undefined,
    observeElementRect: () => undefined,
    overscan: 6,
    scrollMargin,
    scrollToFn: () => undefined,
  });
  const rows = virtualizer.getVirtualItems().map((row) => {
    const localTop = calculateHistoryRowTranslateY(row.start, scrollMargin);
    return {
      bottom: scrollMargin + localTop + row.size - scrollOffset,
      index: row.index,
      top: scrollMargin + localTop - scrollOffset,
    };
  });

  const visibleRows = rows.filter((row) => row.bottom > 0 && row.top < viewportHeight);
  assert.ok(visibleRows.length > 0);
  assert.ok(visibleRows[0]!.top <= 0);
  assert.ok(visibleRows.at(-1)!.bottom >= viewportHeight);

  for (let index = 1; index < rows.length; index += 1) {
    assert.equal(rows[index]!.index, rows[index - 1]!.index + 1);
    assert.equal(rows[index]!.top, rows[index - 1]!.bottom);
  }

  const maximumExpectedRows = Math.ceil(viewportHeight / rowHeight) + 1 + 12;
  assert.ok(
    rows.length <= maximumExpectedRows,
    `${rows.length} virtual rows exceeded the bounded ${maximumExpectedRows}-row viewport window`,
  );
}

test("history margin stays in outer-scroller coordinates across scrolling and preceding layout changes", () => {
  const initialMargin = calculateHistoryScrollMargin(480, 80, 0);
  assert.equal(initialMargin, 400);
  assert.equal(calculateHistoryScrollMargin(-720, 80, 1_200), initialMargin);

  const expandedMargin = calculateHistoryScrollMargin(600, 80, 0);
  assert.equal(expandedMargin, 520);
  assert.equal(calculateHistoryRowTranslateY(initialMargin + 540, initialMargin), 540);
  assert.equal(calculateHistoryRowTranslateY(expandedMargin + 540, expandedMargin), 540);
});

test("list rows continuously cover the viewport without rendering the full history", () => {
  assertVirtualRowsCoverViewport({
    count: 100_000,
    rowHeight: 108,
    scrollMargin: 360,
    scrollOffset: 5_835,
    viewportHeight: 648,
  });
});

test("grid rows continuously cover the viewport with a bounded virtual window", () => {
  const itemCount = 100_000;
  const columns = calculateHistoryGridColumns(920);
  assert.equal(columns, 4);

  assertVirtualRowsCoverViewport({
    count: Math.ceil(itemCount / columns),
    rowHeight: 252,
    scrollMargin: 740,
    scrollOffset: 10_895,
    viewportHeight: 720,
  });
});

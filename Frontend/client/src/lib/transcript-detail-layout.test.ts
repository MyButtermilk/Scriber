import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";

const stylesheet = readFileSync(path.resolve(import.meta.dirname, "../index.css"), "utf8");
const transcriptDetailSource = readFileSync(path.resolve(import.meta.dirname, "../pages/TranscriptDetail.tsx"), "utf8");
const desktopSummaryLayout = stylesheet.slice(
  stylesheet.indexOf("@media (min-width: 1440px)"),
  stylesheet.indexOf("@media (prefers-reduced-motion: reduce)", stylesheet.indexOf("@media (min-width: 1440px)")),
);

test("keeps summary and transcript on the outer app scroller without overlap", () => {
  assert.match(
    desktopSummaryLayout,
    /\.transcript-detail-shell\.has-summary-toc \.summary-toc\s*\{[^}]*position:\s*sticky;/,
  );
  assert.ok(
    transcriptDetailSource.includes("scrollContainerRef={mainScrollRef}"),
    "the table of contents must continue observing the outer app scroller",
  );
  assert.doesNotMatch(
    desktopSummaryLayout,
    /\.transcript-detail-shell\.has-summary-toc \.transcript-summary-panel\[data-state="open"\]\s*\{[^}]*position:\s*sticky;/,
  );
  assert.doesNotMatch(
    desktopSummaryLayout,
    /\.transcript-detail-shell\.has-summary-toc\s+\.transcript-summary-panel[^}]*\{[^}]*overflow-y:\s*(?:auto|scroll)/,
  );
});

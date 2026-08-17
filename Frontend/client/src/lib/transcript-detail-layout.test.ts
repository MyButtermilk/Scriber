import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";

const stylesheet = readFileSync(path.resolve(import.meta.dirname, "../index.css"), "utf8");
const desktopSummaryLayout = stylesheet.slice(
  stylesheet.indexOf("@media (min-width: 1440px)"),
  stylesheet.indexOf("@media (prefers-reduced-motion: reduce)", stylesheet.indexOf("@media (min-width: 1440px)")),
);

test("keeps the complete summary box fixed while only its content scrolls", () => {
  assert.match(
    desktopSummaryLayout,
    /\.transcript-detail-shell\.has-summary-toc \.transcript-summary-panel\[data-state="open"\]\s*\{[^}]*position:\s*sticky;[^}]*max-height:[^;}]+;[^}]*overflow:\s*hidden;/,
  );
  assert.match(
    desktopSummaryLayout,
    /\.transcript-detail-shell\.has-summary-toc \.transcript-summary-panel\s*>\s*\.t-acc-presence\[data-state="open"\]\s*\{[^}]*flex:\s*1 1 auto;[^}]*overflow:\s*hidden;/,
  );
  assert.match(
    desktopSummaryLayout,
    /\.transcript-detail-shell\.has-summary-toc\s+\.transcript-summary-panel\s*>\s*\.t-acc-presence\[data-state="open"\]\s*>\s*\.t-acc-panel\s*>\s*\.t-acc-panel-inner\s*\{[^}]*overflow-y:\s*auto;/,
  );
  assert.doesNotMatch(
    desktopSummaryLayout,
    /\.transcript-detail-shell\.has-summary-toc \.transcript-summary-panel\s*>\s*h3\s*\{[^}]*position:\s*sticky;/,
  );
});

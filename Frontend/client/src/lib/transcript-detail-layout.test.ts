import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";

const stylesheet = readFileSync(path.resolve(import.meta.dirname, "../index.css"), "utf8");
const transcriptDetailSource = readFileSync(path.resolve(import.meta.dirname, "../pages/TranscriptDetail.tsx"), "utf8");
const summaryDocumentSource = readFileSync(
  path.resolve(import.meta.dirname, "../components/transcript-summary-document.tsx"),
  "utf8",
);
const desktopSummaryLayout = stylesheet.slice(
  stylesheet.indexOf("@media (min-width: 1440px)"),
  stylesheet.indexOf("@media (prefers-reduced-motion: reduce)", stylesheet.indexOf("@media (min-width: 1440px)")),
);

test("keeps summary navigation on its own reading viewport with a mobile fallback", () => {
  assert.ok(
    transcriptDetailSource.includes("scrollContainerRef={isMobile ? mainScrollRef : summaryScrollRef}"),
    "desktop contents navigation must observe the inner summary viewport, while mobile retains page scrolling",
  );
  assert.match(
    transcriptDetailSource,
    /<AccordionContent\s+ref=\{summaryScrollRef\}\s+data-summary-scroll="true"/,
    "the existing labelled accordion region must own summary scrolling",
  );
  assert.ok(summaryDocumentSource.includes("root.scrollTo("));
});

test("fits the contents to the reader instead of assuming a single-line header height", () => {
  assert.doesNotMatch(
    desktopSummaryLayout,
    /\.summary-toc\s*\{[^}]*top:\s*[1-9][\d.]*(?:rem|px)/,
    "a fixed header offset cannot accommodate wrapped titles",
  );
});

import assert from "node:assert/strict";
import test from "node:test";

import {
  summaryDocumentLanguage,
  summaryTableOfContentsTitle,
} from "./summary-document-language";

test("table of contents follows an English summary instead of the German UI", () => {
  const summary = "The global order is changing, and this report explains the risks for companies.";
  assert.equal(summaryDocumentLanguage(summary, "de"), "en");
  assert.equal(summaryTableOfContentsTitle(summary, "de"), "Table of Contents");
});

test("table of contents follows a German summary instead of the English UI", () => {
  const summary = "Die globale Ordnung verändert sich, und dieser Bericht ist für Unternehmen wichtig.";
  assert.equal(summaryDocumentLanguage(summary, "en"), "de");
  assert.equal(summaryTableOfContentsTitle(summary, "en"), "Inhaltsverzeichnis");
});

test("short ambiguous summaries use the transcript language hint", () => {
  assert.equal(summaryTableOfContentsTitle("Roadmap", "de-DE"), "Inhaltsverzeichnis");
  assert.equal(summaryTableOfContentsTitle("Roadmap", "auto"), "Table of Contents");
});

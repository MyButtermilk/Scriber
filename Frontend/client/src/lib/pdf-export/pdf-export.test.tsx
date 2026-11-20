import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { inflateSync } from "node:zlib";

import { renderSerializedDoc } from "@formepdf/core";
import { serialize } from "@formepdf/react";
import type { FontRegistration } from "@formepdf/react";
import type { ReactElement } from "react";
import { beforeAll, describe, expect, it } from "vitest";

import type { MeetingExportDocument, TranscriptExportDocument } from "@/lib/api-types";

import { meetingPdfElement, readableParagraphs, transcriptPdfElement } from "./documents";
import type { TranscriptPdfLabels } from "./documents";
import { markdownBlocks, parseSummaryForPdf, transcriptParagraphHeading, transcriptParagraphs } from "./summary-blocks";
import { normalizePdfText, singleLinePdfText, supportsItalic } from "./text";
import { woffToSfnt } from "./woff";

const FONT_DIR = path.resolve(import.meta.dirname, "../../../../node_modules/@fontsource/inter/files");
const PREVIEW_DIR = process.env.SCRIBER_PDF_PREVIEW_DIR || "";

const FACES = [
  ["inter-latin-400-normal.woff", 400, "normal"],
  ["inter-latin-400-italic.woff", 400, "italic"],
  ["inter-latin-500-normal.woff", 500, "normal"],
  ["inter-latin-600-normal.woff", 600, "normal"],
  ["inter-latin-700-normal.woff", 700, "normal"],
  ["inter-latin-700-italic.woff", 700, "italic"],
] as const;

const labels: TranscriptPdfLabels = {
  locale: "de",
  transcript: "Transkript",
  summary: "Zusammenfassung",
  date: "Datum",
  duration: "Dauer",
  pageOf: "Seite {page} von {total}",
  speaker: (speakerNumber) => `Sprecher ${speakerNumber}`,
};

const summaryHtml = `
<section><h2>Größere Märkte, engere Margen</h2>
<p>Der Vortrag ordnet die <strong>Zinswende</strong> für Gewerbeimmobilien ein und zeigt, wo Übergänge teuer werden.</p></section>
<section><h2>Kernaussagen</h2>
<ul><li><strong>Finanzierung:</strong> Anschlussfinanzierungen kosten spürbar mehr.</li>
<li>Nahversorger bleiben <em>stabil</em>; Büroflächen nicht.<ul><li>Leerstand über 12&nbsp;% in B-Lagen</li></ul></li>
<li>Bewertungen folgen den Transaktionen mit Verzögerung → Vorsicht bei Gutachten.</li></ul>
<h3>Zahlen im Überblick</h3>
<table><thead><tr><th>Segment</th><th>Rendite</th><th>Trend</th></tr></thead>
<tbody><tr><td>Nahversorgung</td><td>5,4&nbsp;%</td><td>stabil</td></tr>
<tr><td>Büro (B-Lage)</td><td>6,8&nbsp;%</td><td>steigend</td></tr></tbody></table>
<blockquote><p>„Wer jetzt prolongiert, zahlt für die Geduld der letzten Jahre.“</p></blockquote>
<dl><dt>LTV</dt><dd>Beleihungsauslauf, Verhältnis von Darlehen zu Wert.</dd></dl>
<ol><li>Laufzeiten staffeln</li><li>Covenants prüfen</li></ol>
<pre>ICR = NOI / Zinsaufwand</pre><hr><p>Schlussgedanke mit <code>inline_code</code>.</p></section>
<script>alert(1)</script>`;

const transcript: TranscriptExportDocument = {
  apiVersion: "1",
  kind: "transcript",
  title: "Zinswende und Gewerbeimmobilien – Vortrag vom Frühjahrsforum",
  date: "2026-08-14",
  duration: "42:17",
  content: [
    "[Speaker 1]: Guten Tag, schön, dass Sie da sind. Wir sprechen heute über Finanzierung.",
    "[Speaker 2]: Danke für die Einladung. Fangen wir mit den Zinsen an – „höher für länger“ ist die Lage.",
    "Ein Absatz ohne Sprecher mit Ελληνικά und Кириллица.",
    Array.from({ length: 90 }, (_, index) => `Satz Nummer ${index + 1} beschreibt die Entwicklung der Märkte.`).join(
      " ",
    ),
  ].join("\n\n"),
  summary: summaryHtml,
  summaryFormat: "html",
  filenameStem: "Zinswende",
};

const meeting: MeetingExportDocument = {
  apiVersion: "1",
  kind: "meeting",
  language: "de",
  title: "Roadmap-Abstimmung Q4",
  date: "2026-07-11T09:00:00Z",
  duration: "58:03",
  segmentCount: 3,
  timingQuality: "1 Segment(e) verwenden geschätzte Zeitintervalle",
  labels: {
    meeting: "Besprechung",
    unknown: "Unbekannt",
    executive_summary: "Kurzfassung",
    notes: "Notizen",
    owner: "Verantwortlich",
    due: "Fällig",
    status: "Status",
    date: "Datum",
    duration: "Dauer",
    transcript: "Transkript",
    timestamped_segments: "Segmente mit Zeitstempel",
    timing_quality: "Zeitqualität",
    timestamped_transcript: "Transkript mit Zeitstempeln",
    estimated_timing: "geschätzte Zeit",
    summary: "Zusammenfassung",
    page_of: "Seite {page} von {total}",
  },
  executiveSummary: "Das Team hat die Freigabe für Freitag beschlossen.\n\nOffen bleibt die Überwachung des Rollouts.",
  sections: [
    {
      key: "decisions",
      heading: "Entscheidungen",
      items: [{ text: "Release am Freitag", owner: "", dueDate: "", status: "", statusKey: "" }],
    },
    {
      key: "action_items",
      heading: "Aufgaben",
      items: [
        {
          text: "Kundeninformation versenden",
          owner: "Márta",
          dueDate: "2026-07-12",
          status: "offen",
          statusKey: "open",
        },
        { text: "Monitoring einrichten", owner: "", dueDate: "", status: "erledigt", statusKey: "done" },
      ],
    },
    {
      key: "risks",
      heading: "Risiken",
      items: [{ text: "Kundenfreigabe steht aus", owner: "Alex", dueDate: "", status: "", statusKey: "" }],
    },
  ],
  notes: [{ at: "0:06", body: "Release-Verantwortung klären." }],
  segments: [
    { start: "0:01", end: "0:04", speaker: "Alex", text: "Wir geben den Release für Freitag frei.", estimated: false },
    { start: "0:05", end: "0:08", speaker: "Márta", text: "Ich schicke das Kunden-Update.", estimated: true },
    { start: "0:09", end: "0:15", speaker: "Meeting audio", text: "Einverstanden, danke.", estimated: false },
  ],
  filename: "Roadmap.pdf",
};

let fonts: FontRegistration[] = [];

beforeAll(async () => {
  fonts = await Promise.all(
    FACES.map(async ([file, fontWeight, fontStyle]) => ({
      family: "Inter",
      src: await woffToSfnt(
        new Uint8Array(readFileSync(path.join(FONT_DIR, file))),
        async (compressed) => new Uint8Array(inflateSync(compressed)),
      ),
      fontWeight,
      fontStyle,
    })),
  );
});

async function renderPdf(name: string, element: ReactElement): Promise<string> {
  const bytes = await renderSerializedDoc(serialize(element) as unknown as Record<string, unknown>);
  if (PREVIEW_DIR) {
    mkdirSync(PREVIEW_DIR, { recursive: true });
    writeFileSync(path.join(PREVIEW_DIR, `${name}.pdf`), bytes);
  }
  const text = Buffer.from(bytes).toString("latin1");
  expect(text.startsWith("%PDF-")).toBe(true);
  return text;
}

function pageCount(pdf: string): number {
  return (pdf.match(/\/Type\s*\/Page(?!s)/g) || []).length;
}

describe("summary blocks", () => {
  it("lifts the opening title and standfirst and keeps the summary structure", () => {
    const summary = parseSummaryForPdf(summaryHtml, "html");

    expect(summary.title).toBe("Größere Märkte, engere Margen");
    expect(summary.standfirst.map((inline) => inline.text).join("")).toContain("Zinswende");
    expect(summary.standfirst.some((inline) => inline.bold && inline.text === "Zinswende")).toBe(true);
    expect(summary.blocks.map((block) => block.type)).toEqual([
      "heading",
      "list",
      "heading",
      "table",
      "quote",
      "definitions",
      "list",
      "code",
      "rule",
      "paragraph",
    ]);
    const list = summary.blocks[1];
    expect(list.type === "list" && list.items[1].children[0].items[0].inlines[0].text).toBe(
      "Leerstand über 12\u00a0% in B-Lagen",
    );
    const table = summary.blocks[3];
    expect(table.type === "table" && table.head[0].map((cell) => cell.inlines[0].text)).toEqual([
      "Segment",
      "Rendite",
      "Trend",
    ]);
    expect(JSON.stringify(summary)).not.toContain("alert");
  });

  it("reads legacy Markdown summaries with the DOCX subset", () => {
    const blocks = markdownBlocks("# Titel\n\nText mit **fett** und *kursiv*.\n\n- eins\n- zwei\n\n1. erstens\n\n---");

    expect(blocks.map((block) => block.type)).toEqual(["heading", "paragraph", "list", "list", "rule"]);
    expect(blocks[0].type === "heading" && blocks[0].level).toBe(2);
    expect(blocks[1].type === "paragraph" && blocks[1].inlines).toEqual([
      { text: "Text mit " },
      { text: "fett", bold: true },
      { text: " und " },
      { text: "kursiv", italic: true },
      { text: "." },
    ]);
    expect(blocks[3].type === "list" && blocks[3].ordered).toBe(true);
    expect(parseSummaryForPdf("", "markdown").blocks).toEqual([]);
  });

  it("splits transcript text into speaker paragraphs", () => {
    expect(transcriptParagraphs("[Speaker 2]: Hallo\n\n\nOhne Sprecher\r\n\r\n[Speaker x]: bleibt Text")).toEqual([
      { speakerNumber: 2, text: "Hallo" },
      { speakerNumber: null, text: "Ohne Sprecher" },
      { speakerNumber: null, text: "[Speaker x]: bleibt Text" },
    ]);
  });

  it("keeps canonical [0:12] Speaker 0: turns instead of flattening them", () => {
    const paragraphs = transcriptParagraphs(
      [
        "[0:12] Speaker 0: Willkommen zur Sendung.",
        "[0:15] Speaker 0: Heute geht es um Gezeiten.",
        "[0:21] Speaker 1: Danke für die Einladung.",
        "[125:04] Alice: Eine späte Frage.",
        "[125:09] Alice: Und noch eine.",
      ].join("\n"),
    );
    expect(paragraphs).toEqual([
      { speakerNumber: 0, startLabel: "0:12", text: "Willkommen zur Sendung. Heute geht es um Gezeiten." },
      { speakerNumber: 1, startLabel: "0:21", text: "Danke für die Einladung." },
      { speakerNumber: null, speakerName: "Alice", startLabel: "2:05:04", text: "Eine späte Frage. Und noch eine." },
    ]);
    expect(paragraphs.map((paragraph) => transcriptParagraphHeading(paragraph, labels.speaker))).toEqual([
      "0:12 · Sprecher 0",
      "0:21 · Sprecher 1",
      "2:05:04 · Alice",
    ]);
    expect(transcriptParagraphs("[0:03] Nur ein Zeitstempel.")).toEqual([
      { speakerNumber: null, startLabel: "0:03", text: "Nur ein Zeitstempel." },
    ]);
    expect(transcriptParagraphHeading({ speakerNumber: null, text: "x" }, labels.speaker)).toBe("");
  });
});

describe("PDF text", () => {
  it("replaces glyphs the embedded fonts cannot set and strips controls", () => {
    expect(normalizePdfText("A → B\u0000\u200b ⇐ C\r\nD\u00a0E")).toBe("A › B ‹ C\nD\u00a0E");
    expect(supportsItalic("Größe – „Zitat“ €")).toBe(true);
    expect(supportsItalic("Ελληνικά")).toBe(false);
    expect(singleLinePdfText("  Eine\n lange   Zeile ", 8)).toBe("Eine la…");
  });

  it("only breaks very long paragraphs, at sentence ends, without losing words", () => {
    const short = "Ein Satz. Noch einer.";
    expect(readableParagraphs(short)).toEqual([short]);

    const long = Array.from({ length: 120 }, (_, index) => `Satz ${index} endet hier.`).join(" ");
    const pieces = readableParagraphs(long);
    expect(pieces.length).toBeGreaterThan(1);
    expect(pieces.join(" ")).toBe(long);
    expect(pieces.every((piece) => /[.!?]$/.test(piece))).toBe(true);
  });
});

describe("pdfcn documents", () => {
  it("renders the full transcript export with embedded fonts and a document outline", async () => {
    const pdf = await renderPdf(
      "transcript",
      transcriptPdfElement({ model: transcript, labels, variant: "full", fonts }),
    );

    expect(pageCount(pdf)).toBeGreaterThanOrEqual(2);
    expect(pdf).toContain("/Outlines");
    expect(pdf).toMatch(/\/FontFile2/);
    expect(pdf).toContain("/Lang (de)");
  });

  it("renders a canonical timestamped transcript with Speaker 0 turns", async () => {
    const content = Array.from(
      { length: 40 },
      (_, index) =>
        `[${index}:0${index % 10}] Speaker ${index % 3}: Abschnitt ${index} erklärt die Lage am Markt ausführlich.`,
    ).join("\n");
    const pdf = await renderPdf(
      "transcript-canonical",
      transcriptPdfElement({ model: { ...transcript, content, summary: "" }, labels, variant: "full", fonts }),
    );

    expect(pageCount(pdf)).toBeGreaterThanOrEqual(1);
  });

  it("renders the summary on its own", async () => {
    const pdf = await renderPdf(
      "summary",
      transcriptPdfElement({ model: transcript, labels, variant: "summary", fonts }),
    );

    expect(pageCount(pdf)).toBeGreaterThanOrEqual(1);
    expect(pdf).toContain("/Outlines");
  });

  it("renders a legacy Markdown summary and a transcript without a summary", async () => {
    await renderPdf(
      "summary-markdown",
      transcriptPdfElement({
        model: {
          ...transcript,
          summary: "## Überblick\n\nEin **wichtiger** Punkt.\n\n- eins\n- zwei",
          summaryFormat: "markdown",
        },
        labels,
        variant: "summary",
        fonts,
      }),
    );
    await renderPdf(
      "transcript-plain",
      transcriptPdfElement({ model: { ...transcript, summary: "" }, labels, variant: "full", fonts }),
    );
  });

  it("renders the Meeting export", async () => {
    const pdf = await renderPdf("meeting", meetingPdfElement({ model: meeting, fonts }));

    expect(pageCount(pdf)).toBeGreaterThanOrEqual(1);
    expect(pdf).toContain("/Outlines");
  });
});

import { prepareSummaryHtml } from "@/lib/summary-html";

export interface PdfInline {
  text: string;
  bold?: boolean;
  italic?: boolean;
  code?: boolean;
}

export interface PdfListItem {
  inlines: PdfInline[];
  children: PdfListBlock[];
}

export interface PdfListBlock {
  type: "list";
  ordered: boolean;
  items: PdfListItem[];
}

export interface PdfTableCell {
  inlines: PdfInline[];
  header: boolean;
}

export type PdfBlock =
  | { type: "heading"; level: 2 | 3 | 4; inlines: PdfInline[] }
  | { type: "paragraph"; inlines: PdfInline[] }
  | PdfListBlock
  | { type: "quote"; paragraphs: PdfInline[][] }
  | { type: "code"; text: string }
  | { type: "definitions"; items: Array<{ term: PdfInline[]; description: PdfInline[] }> }
  | { type: "table"; head: PdfTableCell[][]; rows: PdfTableCell[][] }
  | { type: "rule" };

export interface PdfSummary {
  /** Title of the summary's opening section, when the summary carries one. */
  title: string;
  /** Standfirst paragraph that follows the opening title. */
  standfirst: PdfInline[];
  blocks: PdfBlock[];
}

const EMPTY_SUMMARY: PdfSummary = { title: "", standfirst: [], blocks: [] };

type InlineMarks = Omit<PdfInline, "text">;

function pushInline(target: PdfInline[], text: string, marks: InlineMarks): void {
  if (!text) return;
  const previous = target[target.length - 1];
  if (
    previous &&
    Boolean(previous.bold) === Boolean(marks.bold) &&
    Boolean(previous.italic) === Boolean(marks.italic) &&
    Boolean(previous.code) === Boolean(marks.code)
  ) {
    previous.text += text;
    return;
  }
  target.push({ text, ...marks });
}

function trimInlines(inlines: PdfInline[]): PdfInline[] {
  const result = inlines.map((inline) => ({ ...inline }));
  while (result.length && !result[0].text.trimStart()) result.shift();
  while (result.length && !result[result.length - 1].text.trimEnd()) result.pop();
  if (result.length) {
    result[0].text = result[0].text.trimStart();
    result[result.length - 1].text = result[result.length - 1].text.trimEnd();
  }
  return result;
}

export function inlinesToText(inlines: PdfInline[]): string {
  return inlines.map((inline) => inline.text).join("");
}

const BLOCK_CHILD_TAGS = new Set([
  "UL",
  "OL",
  "TABLE",
  "DL",
  "PRE",
  "BLOCKQUOTE",
  "SECTION",
  "P",
  "H2",
  "H3",
  "H4",
  "HR",
]);

function collectInlines(node: Node, marks: InlineMarks, target: PdfInline[], skipBlocks = false): void {
  for (const child of Array.from(node.childNodes)) {
    if (child.nodeType === 3) {
      pushInline(target, (child.textContent || "").replace(/[ \t\n\r\f]+/g, " "), marks);
      continue;
    }
    if (child.nodeType !== 1) continue;
    const element = child as Element;
    const tag = element.tagName;
    if (skipBlocks && BLOCK_CHILD_TAGS.has(tag)) continue;
    if (tag === "BR") pushInline(target, "\n", marks);
    else if (tag === "STRONG") collectInlines(element, { ...marks, bold: true }, target, skipBlocks);
    else if (tag === "EM") collectInlines(element, { ...marks, italic: true }, target, skipBlocks);
    else if (tag === "CODE") collectInlines(element, { ...marks, code: true }, target, skipBlocks);
    else collectInlines(element, marks, target, skipBlocks);
  }
}

function elementInlines(element: Element, skipBlocks = false): PdfInline[] {
  const inlines: PdfInline[] = [];
  collectInlines(element, {}, inlines, skipBlocks);
  return trimInlines(inlines);
}

function childElements(element: Element, ...tags: string[]): Element[] {
  return Array.from(element.children).filter((child) => tags.includes(child.tagName));
}

function listBlock(element: Element): PdfListBlock {
  return {
    type: "list",
    ordered: element.tagName === "OL",
    items: childElements(element, "LI")
      .map((item) => ({
        inlines: elementInlines(item, true),
        children: childElements(item, "UL", "OL").map(listBlock),
      }))
      .filter((item) => item.inlines.length > 0 || item.children.length > 0),
  };
}

function tableRows(scope: Element): PdfTableCell[][] {
  return childElements(scope, "TR")
    .map((row) =>
      childElements(row, "TH", "TD").map((cell) => ({
        inlines: elementInlines(cell),
        header: cell.tagName === "TH",
      })),
    )
    .filter((cells) => cells.length > 0);
}

function tableBlock(element: Element): PdfBlock | null {
  const head: PdfTableCell[][] = [];
  const rows: PdfTableCell[][] = [];
  for (const group of childElements(element, "THEAD")) head.push(...tableRows(group));
  for (const group of childElements(element, "TBODY", "TFOOT")) rows.push(...tableRows(group));
  rows.push(...tableRows(element));
  // A table without <thead> whose first row is all header cells still has one.
  if (!head.length && rows.length > 1 && rows[0].every((cell) => cell.header)) {
    head.push(rows.shift() as PdfTableCell[]);
  }
  return head.length || rows.length ? { type: "table", head, rows } : null;
}

function definitionBlock(element: Element): PdfBlock | null {
  const items: Array<{ term: PdfInline[]; description: PdfInline[] }> = [];
  for (const child of childElements(element, "DT", "DD")) {
    if (child.tagName === "DT") {
      items.push({ term: elementInlines(child), description: [] });
    } else if (items.length && !items[items.length - 1].description.length) {
      items[items.length - 1].description = elementInlines(child);
    } else {
      items.push({ term: [], description: elementInlines(child) });
    }
  }
  return items.length ? { type: "definitions", items } : null;
}

function collectBlocks(container: Element, blocks: PdfBlock[]): void {
  const loose: PdfInline[] = [];
  const flushLoose = () => {
    const inlines = trimInlines(loose.splice(0));
    if (inlines.length) blocks.push({ type: "paragraph", inlines });
  };

  for (const child of Array.from(container.childNodes)) {
    if (child.nodeType === 3) {
      pushInline(loose, (child.textContent || "").replace(/[ \t\n\r\f]+/g, " "), {});
      continue;
    }
    if (child.nodeType !== 1) continue;
    const element = child as Element;
    const tag = element.tagName;
    if (!BLOCK_CHILD_TAGS.has(tag)) {
      const marks: InlineMarks =
        tag === "STRONG" ? { bold: true } : tag === "EM" ? { italic: true } : tag === "CODE" ? { code: true } : {};
      if (tag === "BR") pushInline(loose, "\n", {});
      else collectInlines(element, marks, loose);
      continue;
    }
    flushLoose();
    if (tag === "SECTION") {
      collectBlocks(element, blocks);
    } else if (tag === "H2" || tag === "H3" || tag === "H4") {
      const inlines = elementInlines(element);
      if (inlines.length) blocks.push({ type: "heading", level: Number(tag[1]) as 2 | 3 | 4, inlines });
    } else if (tag === "P") {
      const inlines = elementInlines(element);
      if (inlines.length) blocks.push({ type: "paragraph", inlines });
    } else if (tag === "UL" || tag === "OL") {
      const list = listBlock(element);
      if (list.items.length) blocks.push(list);
    } else if (tag === "BLOCKQUOTE") {
      const nested: PdfBlock[] = [];
      collectBlocks(element, nested);
      const paragraphs = nested.flatMap((block) =>
        block.type === "paragraph" || block.type === "heading" ? [block.inlines] : [],
      );
      if (paragraphs.length) blocks.push({ type: "quote", paragraphs });
    } else if (tag === "PRE") {
      const text = (element.textContent || "").replace(/\s+$/, "");
      if (text.trim()) blocks.push({ type: "code", text });
    } else if (tag === "DL") {
      const definitions = definitionBlock(element);
      if (definitions) blocks.push(definitions);
    } else if (tag === "TABLE") {
      const table = tableBlock(element);
      if (table) blocks.push(table);
    } else if (tag === "HR") {
      blocks.push({ type: "rule" });
    }
  }
  flushLoose();
}

function htmlSummary(value: string): PdfSummary {
  // The stored summary was sanitized when it was saved; it is sanitized again
  // here, exactly as before it is shown in the app, so only the summary
  // element set ever reaches the PDF tree.
  const safeHtml = prepareSummaryHtml(value).html;
  if (!safeHtml) return EMPTY_SUMMARY;
  const body = new DOMParser().parseFromString(`<body>${safeHtml}</body>`, "text/html").body;
  const blocks: PdfBlock[] = [];
  collectBlocks(body, blocks);

  // The summary contract opens with a title and a standfirst. They become the
  // document's own title block instead of repeating below it.
  let title = "";
  let standfirst: PdfInline[] = [];
  const first = blocks[0];
  if (first?.type === "heading" && first.level === 2) {
    title = inlinesToText(first.inlines);
    blocks.shift();
    const next = blocks[0];
    if (next?.type === "paragraph") {
      standfirst = next.inlines;
      blocks.shift();
    }
  }
  return { title, standfirst, blocks };
}

const INLINE_MARKDOWN = /(\*\*\*(.+?)\*\*\*|\*\*(.+?)\*\*|\*(.+?)\*|`(.+?)`)/g;

export function markdownInlines(value: string): PdfInline[] {
  const inlines: PdfInline[] = [];
  let lastIndex = 0;
  for (const match of value.matchAll(INLINE_MARKDOWN)) {
    const index = match.index ?? 0;
    pushInline(inlines, value.slice(lastIndex, index), {});
    if (match[2] !== undefined) pushInline(inlines, match[2], { bold: true, italic: true });
    else if (match[3] !== undefined) pushInline(inlines, match[3], { bold: true });
    else if (match[4] !== undefined) pushInline(inlines, match[4], { italic: true });
    else pushInline(inlines, match[5] ?? "", { code: true });
    lastIndex = index + match[0].length;
  }
  pushInline(inlines, value.slice(lastIndex), {});
  return trimInlines(inlines);
}

/** The same small Markdown subset the DOCX export reads for legacy summaries. */
export function markdownBlocks(value: string): PdfBlock[] {
  const blocks: PdfBlock[] = [];
  let list: PdfListBlock | null = null;
  for (const rawLine of value.replace(/\r\n?/g, "\n").split("\n")) {
    const line = rawLine.trim();
    if (!line) {
      list = null;
      continue;
    }
    const heading = /^(#{1,6})\s+(.+)$/.exec(line);
    if (heading) {
      list = null;
      const level = Math.min(4, Math.max(2, heading[1].length)) as 2 | 3 | 4;
      blocks.push({ type: "heading", level, inlines: markdownInlines(heading[2]) });
      continue;
    }
    const bullet = /^[-*+]\s+(.+)$/.exec(line);
    const numbered = bullet ? null : /^\d+[.)]\s+(.+)$/.exec(line);
    const item = bullet ?? numbered;
    if (item) {
      const ordered = Boolean(numbered);
      if (!list || list.ordered !== ordered) {
        list = { type: "list", ordered, items: [] };
        blocks.push(list);
      }
      list.items.push({ inlines: markdownInlines(item[1]), children: [] });
      continue;
    }
    list = null;
    if (/^(-{3,}|\*{3,}|_{3,})$/.test(line)) blocks.push({ type: "rule" });
    else blocks.push({ type: "paragraph", inlines: markdownInlines(line) });
  }
  return blocks;
}

export function parseSummaryForPdf(summary: string, summaryFormat: string): PdfSummary {
  const value = String(summary || "").trim();
  if (!value) return EMPTY_SUMMARY;
  if (summaryFormat === "html") return htmlSummary(value);
  return { title: "", standfirst: [], blocks: markdownBlocks(value) };
}

export interface PdfTranscriptParagraph {
  speakerNumber: number | null;
  text: string;
}

const SPEAKER_PREFIX = /^\[Speaker (\d+)\]:\s*/;

/** Split stored transcript text into paragraphs with optional speaker numbers. */
export function transcriptParagraphs(content: string): PdfTranscriptParagraph[] {
  return String(content || "")
    .replace(/\r\n?/g, "\n")
    .split(/\n{2,}/)
    .map((paragraph) => paragraph.trim())
    .filter(Boolean)
    .map((paragraph) => {
      const speaker = SPEAKER_PREFIX.exec(paragraph);
      return speaker
        ? { speakerNumber: Number(speaker[1]), text: paragraph.slice(speaker[0].length).trim() }
        : { speakerNumber: null, text: paragraph };
    });
}

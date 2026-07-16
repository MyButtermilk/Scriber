import DOMPurify from "dompurify";

export type SummaryHeadingLevel = 2 | 3 | 4;

export interface SummaryOutlineItem {
  id: string;
  label: string;
  level: SummaryHeadingLevel;
}

export interface PreparedSummaryHtml {
  html: string;
  outline: SummaryOutlineItem[];
  plainText: string;
}

const SUMMARY_TAGS = [
  "a",
  "blockquote",
  "br",
  "code",
  "dd",
  "dl",
  "dt",
  "em",
  "h2",
  "h3",
  "h4",
  "hr",
  "li",
  "ol",
  "p",
  "pre",
  "section",
  "strong",
  "table",
  "tbody",
  "td",
  "tfoot",
  "th",
  "thead",
  "tr",
  "ul",
] as const;

const SUMMARY_FORBIDDEN_TAGS = [
  "audio",
  "base",
  "button",
  "canvas",
  "embed",
  "form",
  "iframe",
  "img",
  "input",
  "link",
  "math",
  "meta",
  "object",
  "option",
  "picture",
  "script",
  "select",
  "slot",
  "source",
  "style",
  "svg",
  "template",
  "textarea",
  "video",
] as const;

const SUMMARY_FORBIDDEN_ATTRIBUTES = [
  "class",
  "onabort",
  "onblur",
  "onchange",
  "onclick",
  "ondblclick",
  "onerror",
  "onfocus",
  "oninput",
  "onkeydown",
  "onkeypress",
  "onkeyup",
  "onload",
  "onmousedown",
  "onmouseenter",
  "onmouseleave",
  "onmousemove",
  "onmouseout",
  "onmouseover",
  "onmouseup",
  "onreset",
  "onscroll",
  "onsubmit",
  "ontouchstart",
  "onwheel",
  "style",
] as const;

const SUMMARY_SAFE_ATTRIBUTES = ["colspan", "href", "rowspan", "scope", "title"] as const;

const EMPTY_PREPARED_SUMMARY: PreparedSummaryHtml = {
  html: "",
  outline: [],
  plainText: "",
};

const SUMMARY_COMBINING_MARKS = new RegExp("\\p{Mark}+", "gu");
const SUMMARY_NON_ALPHANUMERIC = new RegExp("[^\\p{Letter}\\p{Number}]+", "gu");

function stripHtmlCodeFence(value: string): string {
  const trimmed = value.trim();
  const match = /^```(?:html)?\s*\n?([\s\S]*?)\n?```$/i.exec(trimmed);
  return match ? match[1].trim() : trimmed;
}

export function summaryHeadingSlug(value: string): string {
  const slug = value
    .normalize("NFKD")
    .replace(SUMMARY_COMBINING_MARKS, "")
    .toLocaleLowerCase()
    .replace(SUMMARY_NON_ALPHANUMERIC, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 72);
  return slug || "section";
}

export function uniqueSummaryHeadingId(value: string, counts: Map<string, number>): string {
  const base = `summary-${summaryHeadingSlug(value)}`;
  const count = (counts.get(base) || 0) + 1;
  counts.set(base, count);
  return count === 1 ? base : `${base}-${count}`;
}

function childText(node: Node): string {
  return Array.from(node.childNodes, nodeToPlainText).join("");
}

function listItemText(element: Element, index: number, ordered: boolean): string {
  const prefix = ordered ? `${index + 1}. ` : "- ";
  const content = childText(element).trim().replace(/\n{3,}/g, "\n\n");
  return content ? `${prefix}${content}\n` : "";
}

function tableText(element: Element): string {
  return Array.from(element.querySelectorAll(":scope > thead > tr, :scope > tbody > tr, :scope > tfoot > tr, :scope > tr"))
    .map((row) => Array.from(row.querySelectorAll(":scope > th, :scope > td"))
      .map((cell) => childText(cell).trim())
      .join("\t"))
    .filter(Boolean)
    .join("\n");
}

function nodeToPlainText(node: Node): string {
  if (node.nodeType === Node.TEXT_NODE) {
    return node.textContent || "";
  }
  if (node.nodeType !== Node.ELEMENT_NODE) {
    return "";
  }

  const element = node as Element;
  const tag = element.tagName.toLowerCase();
  if (tag === "br") return "\n";
  if (tag === "hr") return "\n\n";
  if (tag === "table") return `${tableText(element)}\n\n`;
  if (tag === "ul" || tag === "ol") {
    return `${Array.from(element.children)
      .filter((child) => child.tagName.toLowerCase() === "li")
      .map((child, index) => listItemText(child, index, tag === "ol"))
      .join("")}\n`;
  }
  if (tag === "li") return childText(element);

  const content = childText(element);
  if (["blockquote", "dd", "dt", "h2", "h3", "h4", "p", "pre"].includes(tag)) {
    return `${content.trim()}\n\n`;
  }
  return content;
}

function sanitizeSummaryFragment(value: string, allowGeneratedIds: boolean): string {
  return DOMPurify.sanitize(value, {
    ALLOW_ARIA_ATTR: false,
    ALLOW_DATA_ATTR: false,
    ALLOWED_ATTR: allowGeneratedIds ? [...SUMMARY_SAFE_ATTRIBUTES, "id"] : [...SUMMARY_SAFE_ATTRIBUTES],
    ALLOWED_TAGS: [...SUMMARY_TAGS],
    FORBID_ATTR: [...SUMMARY_FORBIDDEN_ATTRIBUTES],
    FORBID_TAGS: [...SUMMARY_FORBIDDEN_TAGS],
    KEEP_CONTENT: true,
    RETURN_TRUSTED_TYPE: false,
  });
}

function removeUnsafeSummaryLinks(document: Document): void {
  document.body.querySelectorAll<HTMLAnchorElement>("a[href]").forEach((anchor) => {
    const rawHref = anchor.getAttribute("href");
    if (!rawHref) {
      anchor.removeAttribute("href");
      return;
    }

    try {
      const url = new URL(rawHref);
      if (url.protocol !== "http:" && url.protocol !== "https:") {
        anchor.removeAttribute("href");
      }
    } catch {
      anchor.removeAttribute("href");
    }
  });
}

export function prepareSummaryHtml(value: string): PreparedSummaryHtml {
  if (!value.trim() || typeof DOMParser === "undefined") {
    return EMPTY_PREPARED_SUMMARY;
  }

  const sanitized = sanitizeSummaryFragment(stripHtmlCodeFence(value), false);
  if (!sanitized.trim()) {
    return EMPTY_PREPARED_SUMMARY;
  }

  const document = new DOMParser().parseFromString(sanitized, "text/html");
  removeUnsafeSummaryLinks(document);
  const counts = new Map<string, number>();
  const outline = Array.from(document.body.querySelectorAll<HTMLHeadingElement>("h2, h3, h4"))
    .map((heading): SummaryOutlineItem | null => {
      const label = (heading.textContent || "").replace(/\s+/g, " ").trim();
      if (!label) {
        heading.remove();
        return null;
      }
      const id = uniqueSummaryHeadingId(label, counts);
      heading.id = id;
      return {
        id,
        label,
        level: Number(heading.tagName.slice(1)) as SummaryHeadingLevel,
      };
    })
    .filter((item): item is SummaryOutlineItem => item !== null);

  const html = sanitizeSummaryFragment(document.body.innerHTML, true).trim();
  const plainText = nodeToPlainText(document.body)
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();

  return { html, outline, plainText };
}

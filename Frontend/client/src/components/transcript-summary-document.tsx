import { useCallback, useEffect, useMemo, useState, type MouseEvent, type RefObject } from "react";

import type { PreparedSummaryHtml, SummaryOutlineItem } from "@/lib/summary-html";

interface TranscriptSummaryDocumentProps {
  prepared: PreparedSummaryHtml;
}

interface SummaryTableOfContentsProps {
  outline: SummaryOutlineItem[];
  scrollContainerRef: RefObject<HTMLElement | null>;
}

export function TranscriptSummaryDocument({ prepared }: TranscriptSummaryDocumentProps) {
  if (!prepared.html) {
    return (
      <p className="text-base italic text-muted-foreground">
        This summary did not contain displayable safe HTML.
      </p>
    );
  }

  return (
    <div
      className="summary-document"
      data-summary-format="html"
      dangerouslySetInnerHTML={{ __html: prepared.html }}
    />
  );
}

export function SummaryTableOfContents({ outline, scrollContainerRef }: SummaryTableOfContentsProps) {
  const outlineKey = useMemo(() => outline.map(({ id }) => id).join("|"), [outline]);
  const [activeId, setActiveId] = useState(() => outline[0]?.id || "");

  const resolveActiveHeading = useCallback(() => {
    const root = scrollContainerRef.current;
    if (!root || outline.length === 0) return;

    const atBottom = root.scrollHeight - root.scrollTop - root.clientHeight <= 2;
    if (atBottom) {
      setActiveId(outline.at(-1)?.id || "");
      return;
    }

    const rootTop = root.getBoundingClientRect().top + 24;
    const headings = outline
      .map(({ id }) => document.getElementById(id))
      .filter((heading): heading is HTMLElement => heading !== null);
    const passed = headings.filter((heading) => heading.getBoundingClientRect().top <= rootTop + 1);
    setActiveId((passed.at(-1) || headings[0])?.id || "");
  }, [outlineKey, outline, scrollContainerRef]);

  useEffect(() => {
    setActiveId(outline[0]?.id || "");
  }, [outlineKey, outline]);

  useEffect(() => {
    const root = scrollContainerRef.current;
    if (!root || outline.length === 0 || typeof IntersectionObserver === "undefined") return;

    const headings = outline
      .map(({ id }) => document.getElementById(id))
      .filter((heading): heading is HTMLElement => heading !== null);
    if (headings.length === 0) return;

    const observer = new IntersectionObserver(resolveActiveHeading, {
      root,
      rootMargin: "-24px 0px -70% 0px",
      threshold: [0, 1],
    });
    headings.forEach((heading) => observer.observe(heading));
    resolveActiveHeading();
    return () => observer.disconnect();
  }, [outlineKey, outline, resolveActiveHeading, scrollContainerRef]);

  useEffect(() => {
    let hash = window.location.hash.slice(1);
    try {
      hash = decodeURIComponent(hash);
    } catch {
      return;
    }
    if (!hash || !outline.some(({ id }) => id === hash)) return;
    const frame = window.requestAnimationFrame(() => {
      const heading = document.getElementById(hash);
      if (!heading) return;
      const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
      heading.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
      setActiveId(hash);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [outlineKey, outline]);

  const handleNavigate = useCallback((event: MouseEvent<HTMLAnchorElement>, id: string) => {
    event.preventDefault();
    const heading = document.getElementById(id);
    if (!heading) return;
    const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    heading.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
    window.history.replaceState(window.history.state, "", `#${id}`);
    setActiveId(id);
  }, []);

  if (outline.length < 2) return null;

  return (
    <nav className="summary-toc" aria-label="Table of contents">
      <p className="summary-toc__eyebrow">Contents</p>
      <ol className="summary-toc__list">
        {outline.map((item) => (
          <li key={item.id} className="summary-toc__item" data-level={item.level}>
            <a
              className="summary-toc__link"
              href={`#${item.id}`}
              aria-current={activeId === item.id ? "location" : undefined}
              onClick={(event) => handleNavigate(event, item.id)}
            >
              {item.label}
            </a>
          </li>
        ))}
      </ol>
    </nav>
  );
}

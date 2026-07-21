import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent,
  type RefObject,
} from "react";

import type { PreparedSummaryHtml, SummaryOutlineItem } from "@/lib/summary-html";
import { useI18n } from "@/i18n";

interface TranscriptSummaryDocumentProps {
  prepared: PreparedSummaryHtml;
}

interface SummaryTableOfContentsProps {
  outline: SummaryOutlineItem[];
  scrollContainerRef: RefObject<HTMLElement | null>;
  title: string;
}

interface SummaryTocPathGeometry {
  activeLengths: Record<string, number>;
  d: string;
  height: number;
  totalLength: number;
}

const EMPTY_TOC_PATH: SummaryTocPathGeometry = {
  activeLengths: {},
  d: "",
  height: 0,
  totalLength: 0,
};

const TOC_PATH_X_BY_LEVEL: Record<SummaryOutlineItem["level"], number> = {
  2: 8,
  3: 20,
  4: 32,
};

function measureTocPath(
  outline: SummaryOutlineItem[],
  list: HTMLOListElement,
  itemElements: Map<string, HTMLLIElement>,
): SummaryTocPathGeometry {
  const listRect = list.getBoundingClientRect();
  const points = outline.flatMap((item) => {
    const element = itemElements.get(item.id);
    if (!element) return [];
    const rect = element.getBoundingClientRect();
    return [{
      id: item.id,
      x: TOC_PATH_X_BY_LEVEL[item.level],
      y: rect.top - listRect.top + rect.height / 2,
    }];
  });
  if (points.length === 0) return EMPTY_TOC_PATH;

  const commands: string[] = [`M ${points[0].x} 0`];
  const activeLengths: Record<string, number> = {};
  let currentX = points[0].x;
  let currentY = 0;
  let totalLength = 0;

  const lineTo = (x: number, y: number) => {
    totalLength += Math.hypot(x - currentX, y - currentY);
    commands.push(`L ${x} ${y}`);
    currentX = x;
    currentY = y;
  };

  points.forEach((point) => {
    if (point.x !== currentX) {
      const midpoint = currentY + (point.y - currentY) / 2;
      const bendHalfHeight = Math.min(5, Math.max(2, (point.y - currentY) * 0.12));
      lineTo(currentX, midpoint - bendHalfHeight);
      lineTo(point.x, midpoint + bendHalfHeight);
    }
    lineTo(point.x, point.y);
    activeLengths[point.id] = totalLength;
  });

  const height = Math.max(Math.ceil(list.scrollHeight), Math.ceil(currentY));
  return {
    activeLengths,
    d: commands.join(" "),
    height,
    totalLength,
  };
}

export function TranscriptSummaryDocument({ prepared }: TranscriptSummaryDocumentProps) {
  const { t } = useI18n();
  if (!prepared.html) {
    return (
      <p className="text-base italic text-muted-foreground">
        {t("This summary did not contain displayable safe HTML.")}
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

export function SummaryTableOfContents({ outline, scrollContainerRef, title }: SummaryTableOfContentsProps) {
  const outlineKey = useMemo(() => outline.map(({ id }) => id).join("|"), [outline]);
  const [activeId, setActiveId] = useState(() => outline[0]?.id || "");
  const [pathGeometry, setPathGeometry] = useState<SummaryTocPathGeometry>(EMPTY_TOC_PATH);
  const navRef = useRef<HTMLElement | null>(null);
  const listRef = useRef<HTMLOListElement | null>(null);
  const itemRefs = useRef(new Map<string, HTMLLIElement>());
  const navigationTargetRef = useRef("");
  const navigationReleaseTimerRef = useRef<number | null>(null);

  const resolveActiveHeading = useCallback(() => {
    const root = scrollContainerRef.current;
    if (!root || outline.length === 0) return;

    const navigationTarget = navigationTargetRef.current;
    if (navigationTarget && outline.some(({ id }) => id === navigationTarget)) {
      setActiveId(navigationTarget);
      return;
    }

    const isScrollable = root.scrollHeight > root.clientHeight + 2;
    const atBottom = isScrollable && root.scrollHeight - root.scrollTop - root.clientHeight <= 2;
    if (atBottom) {
      setActiveId(outline.at(-1)?.id || "");
      return;
    }

    const activationOffset = Math.min(136, Math.max(72, root.clientHeight * 0.18));
    const activationTop = root.getBoundingClientRect().top + activationOffset;
    const headings = outline
      .map(({ id }) => document.getElementById(id))
      .filter((heading): heading is HTMLElement => heading !== null);
    const passed = headings.filter((heading) => heading.getBoundingClientRect().top <= activationTop + 1);
    setActiveId((passed.at(-1) || headings[0])?.id || "");
  }, [outlineKey, outline, scrollContainerRef]);

  const releaseNavigationTarget = useCallback((expectedId: string) => {
    if (navigationTargetRef.current !== expectedId) return;
    navigationTargetRef.current = "";
    navigationReleaseTimerRef.current = null;

    const root = scrollContainerRef.current;
    const heading = document.getElementById(expectedId);
    if (!root || !heading) {
      resolveActiveHeading();
      return;
    }
    const rootRect = root.getBoundingClientRect();
    const headingRect = heading.getBoundingClientRect();
    const targetIsVisible = headingRect.bottom >= rootRect.top + 24
      && headingRect.top <= rootRect.bottom - 24;
    if (!targetIsVisible) resolveActiveHeading();
  }, [resolveActiveHeading, scrollContainerRef]);

  const scheduleNavigationRelease = useCallback((delayMs: number) => {
    const expectedId = navigationTargetRef.current;
    if (!expectedId) return;
    if (navigationReleaseTimerRef.current !== null) {
      window.clearTimeout(navigationReleaseTimerRef.current);
    }
    navigationReleaseTimerRef.current = window.setTimeout(
      () => releaseNavigationTarget(expectedId),
      delayMs,
    );
  }, [releaseNavigationTarget]);

  const holdNavigationTarget = useCallback((id: string) => {
    navigationTargetRef.current = id;
    setActiveId(id);
    scheduleNavigationRelease(4_000);
  }, [scheduleNavigationRelease]);

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

    let observer: IntersectionObserver | null = null;
    const connectObserver = () => {
      observer?.disconnect();
      const activationOffset = Math.min(136, Math.max(72, root.clientHeight * 0.18));
      const bottomMargin = Math.max(0, root.clientHeight - activationOffset - 2);
      observer = new IntersectionObserver(resolveActiveHeading, {
        root,
        rootMargin: `-${activationOffset}px 0px -${bottomMargin}px 0px`,
        threshold: 0,
      });
      headings.forEach((heading) => observer?.observe(heading));
      resolveActiveHeading();
    };

    connectObserver();
    const resizeObserver = typeof ResizeObserver === "undefined"
      ? null
      : new ResizeObserver(connectObserver);
    resizeObserver?.observe(root);
    return () => {
      resizeObserver?.disconnect();
      observer?.disconnect();
    };
  }, [outlineKey, outline, resolveActiveHeading, scrollContainerRef]);

  useEffect(() => {
    const root = scrollContainerRef.current;
    if (!root || outline.length === 0) return;

    let frame = 0;
    const scheduleResolve = () => {
      if (navigationTargetRef.current) {
        scheduleNavigationRelease(180);
        return;
      }
      if (frame !== 0) return;
      frame = window.requestAnimationFrame(() => {
        frame = 0;
        resolveActiveHeading();
      });
    };

    root.addEventListener("scroll", scheduleResolve, { passive: true });
    scheduleResolve();
    return () => {
      root.removeEventListener("scroll", scheduleResolve);
      window.cancelAnimationFrame(frame);
    };
  }, [outlineKey, outline, resolveActiveHeading, scheduleNavigationRelease, scrollContainerRef]);

  useEffect(() => () => {
    if (navigationReleaseTimerRef.current !== null) {
      window.clearTimeout(navigationReleaseTimerRef.current);
    }
  }, []);

  useLayoutEffect(() => {
    const list = listRef.current;
    if (!list || outline.length === 0) {
      setPathGeometry(EMPTY_TOC_PATH);
      return;
    }

    let frame = 0;
    const measure = () => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => {
        const nextGeometry = measureTocPath(outline, list, itemRefs.current);
        setPathGeometry((current) => current.d === nextGeometry.d
          && current.height === nextGeometry.height
          && current.totalLength === nextGeometry.totalLength
          ? current
          : nextGeometry);
      });
    };

    measure();
    const resizeObserver = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(measure);
    resizeObserver?.observe(list);
    itemRefs.current.forEach((element) => resizeObserver?.observe(element));
    return () => {
      resizeObserver?.disconnect();
      window.cancelAnimationFrame(frame);
    };
  }, [outlineKey, outline]);

  useEffect(() => {
    const nav = navRef.current;
    const item = itemRefs.current.get(activeId);
    if (!nav || !item) return;

    const frame = window.requestAnimationFrame(() => {
      const navRect = nav.getBoundingClientRect();
      const itemRect = item.getBoundingClientRect();
      if (itemRect.top >= navRect.top + 28 && itemRect.bottom <= navRect.bottom - 16) return;
      const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
      nav.scrollTo({
        behavior: reduceMotion ? "auto" : "smooth",
        top: nav.scrollTop + itemRect.top - navRect.top - nav.clientHeight / 2 + itemRect.height / 2,
      });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [activeId]);

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
      holdNavigationTarget(hash);
      heading.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [holdNavigationTarget, outlineKey, outline]);

  const handleNavigate = useCallback((event: MouseEvent<HTMLAnchorElement>, id: string) => {
    event.preventDefault();
    const heading = document.getElementById(id);
    if (!heading) return;
    const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    holdNavigationTarget(id);
    heading.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
    window.history.replaceState(window.history.state, "", `#${id}`);
  }, [holdNavigationTarget]);

  if (outline.length < 2) return null;

  const activePathLength = pathGeometry.activeLengths[activeId]
    ?? pathGeometry.activeLengths[outline[0]?.id || ""]
    ?? 0;

  return (
    <nav ref={navRef} className="summary-toc" aria-label={title}>
      <p className="summary-toc__eyebrow">{title}</p>
      <ol ref={listRef} className="summary-toc__list">
        {pathGeometry.d && (
          <svg
            className="summary-toc__path"
            width="44"
            height={pathGeometry.height}
            viewBox={`0 0 44 ${pathGeometry.height}`}
            aria-hidden="true"
            focusable="false"
          >
            <path className="summary-toc__path-base" d={pathGeometry.d} />
            <path
              className="summary-toc__path-active"
              d={pathGeometry.d}
              style={{ strokeDasharray: `${activePathLength} ${Math.max(pathGeometry.totalLength, 1)}` }}
            />
          </svg>
        )}
        {outline.map((item) => (
          <li
            key={item.id}
            ref={(element) => {
              if (element) itemRefs.current.set(item.id, element);
              else itemRefs.current.delete(item.id);
            }}
            className="summary-toc__item"
            data-level={item.level}
          >
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

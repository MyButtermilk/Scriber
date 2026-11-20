import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * Page sections share one accent hue. The eyebrow rule, focus glows and the
 * primary action on each page pick it up through `--page-accent`.
 */
export type PageAccent = "voice" | "meetings" | "youtube" | "file" | "podcasts" | "settings";

interface PageIntroProps {
  eyebrow: string;
  title: string;
  description: string;
  accent?: PageAccent;
  titleAccessory?: ReactNode;
  actions?: ReactNode;
  bottomContent?: ReactNode;
  sticky?: boolean;
  className?: string;
}

// Hysteresis keeps the header from flickering around a single threshold.
const CONDENSE_AT = 40;
const EXPAND_AT = 8;

export function PageIntro({
  eyebrow,
  title,
  description,
  accent = "voice",
  titleAccessory,
  actions,
  bottomContent,
  sticky = true,
  className,
}: PageIntroProps) {
  const headerRef = useRef<HTMLElement | null>(null);
  const descriptionRef = useRef<HTMLParagraphElement | null>(null);
  const condensedRef = useRef(false);
  const expandedHeightRef = useRef(0);
  const [condensed, setCondensed] = useState(false);
  condensedRef.current = condensed;

  useEffect(() => {
    if (!sticky) return;
    const header = headerRef.current;
    const viewport = header?.closest<HTMLElement>('[data-app-scroll-container="true"]');
    if (!header || !viewport) return;
    let frame = 0;
    const update = () => {
      frame = 0;
      const top = viewport.scrollTop;
      setCondensed((previous) => (previous ? top > EXPAND_AT : top > CONDENSE_AT));
    };
    const schedule = () => {
      if (!frame) frame = window.requestAnimationFrame(update);
    };
    update();
    viewport.addEventListener("scroll", schedule, { passive: true });
    return () => {
      viewport.removeEventListener("scroll", schedule);
      if (frame) window.cancelAnimationFrame(frame);
    };
  }, [sticky]);

  // While the header shrinks, a matching bottom margin keeps its footprint in
  // the document constant, so the content below never jumps. ResizeObserver
  // runs after layout and before paint, which keeps the compensation in step
  // with every frame of the transition.
  useLayoutEffect(() => {
    if (!sticky) return;
    const header = headerRef.current;
    if (!header || typeof ResizeObserver === "undefined") return;
    const sync = () => {
      const height = header.offsetHeight;
      if (!condensedRef.current) {
        expandedHeightRef.current = height;
        header.style.setProperty("--page-intro-compensation", "0px");
        const descriptionBlock = (descriptionRef.current?.offsetHeight ?? 0) + 12;
        // Estimate for callers that need the stuck height before the first condense.
        header.dataset.condensedHeight = String(Math.max(0, Math.round(height - descriptionBlock - 22)));
        return;
      }
      header.style.setProperty("--page-intro-compensation", `${Math.max(0, expandedHeightRef.current - height)}px`);
      header.dataset.condensedHeight = String(height);
    };
    sync();
    const observer = new ResizeObserver(sync);
    observer.observe(header);
    return () => observer.disconnect();
  }, [sticky]);

  useLayoutEffect(() => {
    const header = headerRef.current;
    if (!header || condensed) return;
    header.style.setProperty("--page-intro-compensation", "0px");
  }, [condensed]);

  return (
    <header
      ref={headerRef}
      data-page-intro="true"
      data-page-accent={accent}
      data-condensed={condensed ? "true" : "false"}
      style={{ "--page-intro-compensation": "0px" } as CSSProperties}
      className={cn(
        "page-intro transcription-intro text-left",
        sticky ? "page-intro--sticky" : "relative z-0",
        bottomContent && "page-intro--with-bottom",
        className,
      )}
    >
      <div className={cn("grid min-w-0 gap-4", actions && "md:grid-cols-[minmax(0,1fr)_auto] md:items-start")}>
        <div className="min-w-0">
          <div className="page-intro__eyebrow flex min-w-0 items-center gap-2.5 text-ui-micro font-semibold uppercase tracking-[0.18em] text-muted-foreground">
            <span className="page-intro__rule h-px w-7 shrink-0" aria-hidden="true" />
            <span className="break-words text-pretty">{eyebrow}</span>
          </div>

          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <h1 className="page-intro__title break-words text-balance font-heading font-semibold text-foreground">
              {title}
            </h1>
            {titleAccessory ? <div className="shrink-0">{titleAccessory}</div> : null}
          </div>

          <div className="page-intro__description" aria-hidden={condensed ? "true" : undefined}>
            <div className="min-h-0 overflow-hidden">
              <p
                ref={descriptionRef}
                className="max-w-none break-words text-pretty text-[13px] leading-[1.55] text-muted-foreground md:text-[14px]"
              >
                {description}
              </p>
            </div>
          </div>
        </div>

        {actions ? (
          <div className="page-intro__actions flex flex-wrap items-center gap-2 md:justify-end">{actions}</div>
        ) : null}
      </div>

      {bottomContent ? <div className="page-intro__bottom">{bottomContent}</div> : null}
    </header>
  );
}

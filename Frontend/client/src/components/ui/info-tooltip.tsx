import { useEffect, useId, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Info } from "lucide-react";

import { cn } from "@/lib/utils";

type InfoTooltipProps = {
  label: string;
  children: ReactNode;
  className?: string;
  compact?: boolean;
};

/** Secondary guidance stays discoverable by mouse, keyboard, and touch. */
export function InfoTooltip({ label, children, className, compact = false }: InfoTooltipProps) {
  const id = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ left: 0, top: 0, width: 340, below: false });

  const cancelClose = () => {
    if (closeTimer.current) clearTimeout(closeTimer.current);
    closeTimer.current = null;
  };
  const reveal = () => {
    cancelClose();
    setOpen(true);
  };
  const scheduleClose = () => {
    cancelClose();
    closeTimer.current = setTimeout(() => {
      if (document.activeElement !== triggerRef.current) setOpen(false);
    }, 100);
  };

  useLayoutEffect(() => {
    if (!open || !triggerRef.current) return;
    const rect = triggerRef.current.getBoundingClientRect();
    const width = Math.min(340, Math.max(160, window.innerWidth - 24));
    const height = panelRef.current?.getBoundingClientRect().height || 160;
    const below = rect.top < height + 20;
    setPosition({
      left: Math.max(12, Math.min(rect.left, window.innerWidth - width - 12)),
      top: below ? Math.max(12, Math.min(rect.bottom + 8, window.innerHeight - height - 12)) : rect.top - 8,
      width,
      below,
    });
  }, [open, children]);

  useEffect(() => {
    if (!open) return;
    const dismiss = () => setOpen(false);
    const onScroll = (event: Event) => {
      // Long guidance may scroll inside its own viewport-bounded panel.
      if (event.target instanceof Node && panelRef.current?.contains(event.target)) return;
      dismiss();
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        dismiss();
      }
    };
    const onOutside = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!triggerRef.current?.contains(target) && !panelRef.current?.contains(target)) dismiss();
    };
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("pointerdown", onOutside);
    window.addEventListener("resize", dismiss);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      document.removeEventListener("pointerdown", onOutside);
      window.removeEventListener("resize", dismiss);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [open]);
  useEffect(
    () => () => {
      if (closeTimer.current) clearTimeout(closeTimer.current);
    },
    [],
  );

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className={cn("info-tooltip-trigger", compact && "is-compact", className)}
        aria-label={label}
        aria-describedby={open ? id : undefined}
        onMouseEnter={reveal}
        onMouseLeave={scheduleClose}
        onFocus={reveal}
        onBlur={() => {
          cancelClose();
          setOpen(false);
        }}
        onClick={reveal}
      >
        <Info aria-hidden="true" size={14} strokeWidth={1.65} />
        {!compact && <span>{label}</span>}
      </button>
      {open &&
        createPortal(
          <div
            ref={panelRef}
            id={id}
            role="tooltip"
            className="info-tooltip-panel"
            data-side={position.below ? "bottom" : "top"}
            style={{ left: position.left, top: position.top, width: position.width }}
            onMouseEnter={cancelClose}
            onMouseLeave={scheduleClose}
          >
            <p className="info-tooltip-title">{label}</p>
            <div className="info-tooltip-copy">{children}</div>
          </div>,
          document.body,
        )}
    </>
  );
}

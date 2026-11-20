import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

interface CountBadgeProps {
  /** Already formatted for the locale, e.g. formatNumber(total). */
  value: string;
  /** Accessible label, e.g. "5,317 recordings". */
  label?: string;
  className?: string;
}

/** The one count chip used next to section titles across the app. */
export function CountBadge({ value, label, className }: CountBadgeProps) {
  return (
    <span
      className={cn(
        "section-count-badge inline-flex h-6 min-w-6 items-center justify-center rounded-full px-2 text-[11px] font-semibold tabular-nums text-muted-foreground",
        className,
      )}
      aria-label={label}
    >
      {value}
    </span>
  );
}

interface SectionHeadingProps {
  title: string;
  /** Formatted count shown as a CountBadge. */
  count?: string;
  countLabel?: string;
  description?: ReactNode;
  /** Controls aligned to the end of the heading row (search, filters…). */
  actions?: ReactNode;
  /** "page" for main-column sections, "rail" for side columns and lists. */
  size?: "page" | "rail";
  as?: "h2" | "h3";
  id?: string;
  className?: string;
}

/**
 * Shared heading for content sections ("Recent recordings", "Your shows",
 * "Saved meetings"): title + count chip, optional one-line description and
 * trailing actions. Keeps type scale and spacing identical everywhere.
 */
export function SectionHeading({
  title,
  count,
  countLabel,
  description,
  actions,
  size = "page",
  as: Heading = "h2",
  id,
  className,
}: SectionHeadingProps) {
  return (
    <div
      className={cn(
        "section-heading flex min-w-0 flex-col gap-3",
        actions && "lg:flex-row lg:items-end lg:justify-between",
        className,
      )}
    >
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2.5">
          <Heading
            id={id}
            className={cn(
              "font-heading font-semibold text-foreground",
              size === "page" ? "text-[20px] tracking-[-0.02em]" : "text-[15px] tracking-[-0.01em]",
            )}
          >
            {title}
          </Heading>
          {count !== undefined ? <CountBadge value={count} label={countLabel} /> : null}
        </div>
        {description ? (
          <p
            className={cn(
              "mt-1 max-w-[58ch] text-pretty text-muted-foreground",
              size === "page" ? "text-[12px] leading-4" : "text-[11.5px] leading-4",
            )}
          >
            {description}
          </p>
        ) : null}
      </div>
      {actions}
    </div>
  );
}

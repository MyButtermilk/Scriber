import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

interface PageIntroProps {
  eyebrow: string;
  title: string;
  description: string;
  accentClassName?: string;
  titleAccessory?: ReactNode;
  actions?: ReactNode;
  bottomContent?: ReactNode;
  sticky?: boolean;
  className?: string;
}

export function PageIntro({
  eyebrow,
  title,
  description,
  accentClassName = "bg-primary/60",
  titleAccessory,
  actions,
  bottomContent,
  sticky = true,
  className,
}: PageIntroProps) {
  return (
    <header
      data-page-intro="true"
      className={cn(
        "transcription-intro mb-6 border-b border-border/65 px-1 pb-5 pt-1 text-left md:pb-6",
        sticky ? "sticky top-3 z-20" : "relative z-0",
        bottomContent && "pb-0 md:pb-0",
        className,
      )}
    >
      <div className={cn("min-w-0", actions && "md:flex md:items-start md:justify-between md:gap-8")}>
        <div className="min-w-0 flex-1">
          <div
            className={cn(
              "mb-2.5 flex min-w-0 items-center gap-2.5 pt-1 text-ui-micro font-semibold uppercase",
              "tracking-[0.16em] text-muted-foreground",
            )}
          >
            <span className={cn("h-px w-6 shrink-0", accentClassName)} aria-hidden="true" />
            <span>{eyebrow}</span>
          </div>

          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <h1
              className={cn(
                "text-balance font-heading text-[36px] font-semibold leading-[1.02]",
                "tracking-[-0.036em] text-foreground md:text-[42px]",
              )}
            >
              {title}
            </h1>
            {titleAccessory ? <div className="shrink-0">{titleAccessory}</div> : null}
          </div>

          <p className="mt-3 max-w-[72ch] text-pretty text-[13px] leading-5 text-muted-foreground md:text-[13.5px]">
            {description}
          </p>
        </div>

        {actions ? <div className="mt-4 flex shrink-0 flex-wrap items-center gap-2 md:mt-0">{actions}</div> : null}
      </div>

      {bottomContent ? <div className="mt-5 border-t border-border/55 py-2.5">{bottomContent}</div> : null}
    </header>
  );
}

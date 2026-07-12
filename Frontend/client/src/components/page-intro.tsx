import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

interface PageIntroProps {
  eyebrow: string;
  title: string;
  description: string;
  accentClassName?: string;
  titleAccessory?: ReactNode;
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
  bottomContent,
  sticky = true,
  className,
}: PageIntroProps) {
  return (
    <header
      className={cn(
        "transcription-intro -mx-4 -mt-5 mb-6 border-b border-slate-200/80 bg-card/95 px-4 pt-5 text-left backdrop-blur-xl dark:border-white/[0.065] md:-mx-6 md:-mt-6 md:px-6 md:pt-6",
        sticky ? "sticky top-0 z-20" : "relative z-0",
        bottomContent ? "pb-0" : "pb-4",
        className,
      )}
    >
      <div className="mb-3 flex items-center gap-2.5 text-[9.5px] font-semibold uppercase tracking-[0.22em] text-muted-foreground">
        <span className={cn("h-px w-7 shrink-0", accentClassName)} aria-hidden="true" />
        <span>{eyebrow}</span>
      </div>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <h1 className="text-balance font-heading text-[36px] font-semibold leading-[0.96] tracking-[-0.045em] text-foreground md:text-[42px]">
          {title}
        </h1>
        {titleAccessory ? <div className="shrink-0">{titleAccessory}</div> : null}
      </div>

      <p className="mt-3 max-w-[65ch] text-pretty text-[13px] leading-5 text-muted-foreground md:text-[13.5px]">
        {description}
      </p>

      {bottomContent ? (
        <div className="-mx-4 mt-4 border-t border-slate-200/80 px-4 py-2 dark:border-white/[0.065] md:-mx-6 md:px-6">
          {bottomContent}
        </div>
      ) : null}
    </header>
  );
}
